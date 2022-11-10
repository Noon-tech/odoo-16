from odoo import api, Command, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.osv.expression import get_unaccent_wrapper

from odoo.addons.base.models.res_bank import sanitize_account_number

from xmlrpc.client import MAXINT


class AccountBankStatementLine(models.Model):
    _name = "account.bank.statement.line"
    _inherits = {'account.move': 'move_id'}
    _description = "Bank Statement Line"
    _order = "internal_index desc"
    _check_company_auto = True

    # FIXME: Field having the same name in both tables are confusing (partner_id). We don't change it because:
    # - It's a mess to track/fix.
    # - Some fields here could be simplified when the onchanges will be gone in account.move.
    # Should be improved in the future.
    # - there should be a better way for syncing account_moves with bank transactions, payments, invoices, etc.

    # == Business fields ==
    def default_get(self, fields_list):
        defaults = super().default_get(fields_list)
        # copy the date and statement from the latest transaction of the same journal to help the user
        # to enter the next transaction, they do not have to enter the date and the statement every time until the
        # statement is completed. It is only possible if we know the journal that is used, so it can only be done
        # in a view in which the journal is already set and so is single journal view.
        if'journal_id' in defaults:
            last_line = self.search([('journal_id', '=', defaults.get('journal_id'))], limit=1)
            statement = last_line.statement_id
            if statement and not statement.is_complete:
                defaults.setdefault(
                    'statement_id', statement.id
                )
                if statement.date:
                    defaults.setdefault(
                        'date', statement.date
                    )

        return defaults

    move_id = fields.Many2one(
        comodel_name='account.move',
        auto_join=True,
        string='Journal Entry', required=True, readonly=True, ondelete='cascade',
        check_company=True)
    statement_id = fields.Many2one(
        comodel_name='account.bank.statement',
        string='Statement',
    )

    # Payments generated during the reconciliation of this bank statement lines.
    payment_ids = fields.Many2many(
        comodel_name='account.payment',
        relation='account_payment_account_bank_statement_line_rel',
        string='Auto-generated Payments',
    )

    # This sequence is working reversed because the default order is reversed, more info in compute_internal_index
    sequence = fields.Integer(default=1)
    partner_id = fields.Many2one(
        comodel_name='res.partner',
        string='Partner', ondelete='restrict',
        domain="['|', ('parent_id','=', False), ('is_company','=',True)]",
        check_company=True)

    # Technical field used to store the bank account number before its creation, upon the line's processing
    account_number = fields.Char(string='Bank Account Number')

    # This field is used to record the third party name when importing bank statement in electronic format,
    # when the partner doesn't exist yet in the database (or cannot be found).
    partner_name = fields.Char()

    # Transaction type is used in electronic format, when the type of transaction is available in the imported file.
    transaction_type = fields.Char()
    payment_ref = fields.Char(string='Label')
    currency_id = fields.Many2one(
        comodel_name='res.currency',
        string='Journal Currency',
        compute='_compute_currency_id', store=True,
    )
    amount = fields.Monetary()

    # Note the values of this field does not necessarily correspond to the cumulated balance in the account move line.
    # here these values correspond to occurrence order (the reality) and they should match the bank report but in
    # the move lines, it corresponds to the recognition order. Also, the statements act as checkpoints on this field
    running_balance = fields.Monetary(
        compute='_compute_running_balance'
    )
    foreign_currency_id = fields.Many2one(
        comodel_name='res.currency',
        string="Foreign Currency",
        help="The optional other currency if it is a multi-currency entry.",
    )
    amount_currency = fields.Monetary(
        string="Amount in Currency",
        currency_field='foreign_currency_id',
        help="The amount expressed in an optional other currency if it is a multi-currency entry.",
    )

    # == Technical fields ==
    # The amount left to be reconciled on this statement line (signed according to its move lines' balance),
    # expressed in its currency. This is a technical field use to speed up the application of reconciliation models.
    amount_residual = fields.Float(
        string="Residual Amount",
        compute="_compute_is_reconciled",
        store=True,
    )
    country_code = fields.Char(
        related='company_id.account_fiscal_country_id.code'
    )

    # Technical field used to store the internal reference of the statement line for fast indexing and easier comparing
    # of statement lines. It holds the combination of the date, sequence and id of each line. Without this field,
    # the search/sorting lines would be very slow. The date field is related and stored in the account.move model,
    # so it is not possible to have an index on it (unless we use a sql view which is too complicated).
    # Using this prevents us having a compound index, and extensive `where` clauses.
    # Without this finding lines before current line (which we need e.g. for calculating the running balance)
    # would need a query like this:
    #   date < current date OR (date = current date AND sequence > current date) or (
    #   date = current date AND sequence = current sequence AND id < current id)
    # which needs to be repeated all over the code.
    # This would be simply "internal index < current internal index" using this field.
    internal_index = fields.Char(
        string='Internal Reference',
        compute='_compute_internal_index', store=True,
        index=True,
    )

    # Technical field indicating if the statement line is already reconciled.
    is_reconciled = fields.Boolean(
        string='Is Reconciled',
        compute='_compute_is_reconciled', store=True,
    )
    statement_complete = fields.Boolean(
        related='statement_id.is_complete',
    )
    statement_valid = fields.Boolean(
        related='statement_id.is_valid',
    )

    # -------------------------------------------------------------------------
    # COMPUTE METHODS
    # -------------------------------------------------------------------------

    @api.depends('journal_id.currency_id')
    def _compute_currency_id(self):
        for st_line in self:
            st_line.currency_id = st_line.journal_id.currency_id or st_line.company_id.currency_id

    def _compute_running_balance(self):
        # It looks back to find the latest statement and uses its balance_start as an anchor point for calculation, so
        # that the running balance is always relative to the latest statement. In this way we do not need to calculate
        # the running balance for all statement lines every time.
        # If there are statements inside the computed range, their balance_start has priority over calculated balance.

        self.statement_id.flush_model(['balance_start', 'first_line_index'])
        self.flush_model(['internal_index', 'date', 'journal_id', 'statement_id', 'amount'])
        record_by_id = {x.id: x for x in self}

        for journal in self.journal_id:
            journal_lines = self.filtered(lambda line: line.journal_id == journal).sorted('internal_index')
            max_index = max(journal_lines.mapped('internal_index'))
            min_index = min(journal_lines.mapped('internal_index'))

            # Find the oldest index for each journal.
            self._cr.execute(
                """
                    SELECT first_line_index, balance_start
                    FROM account_bank_statement
                    WHERE
                        first_line_index < %s
                        AND journal_id = %s
                    ORDER BY first_line_index DESC
                    LIMIT 1
                """,
                [min_index, journal.id],
            )
            current_running_balance = 0.0
            extra_clause = ''
            extra_params = []
            row = self._cr.fetchone()
            if row:
                starting_index, current_running_balance = row
                extra_clause = "AND st_line.internal_index >= %s"
                extra_params.append(starting_index)

            self._cr.execute(
                f"""
                    SELECT
                        st_line.id,
                        st_line.amount,
                        st.first_line_index = st_line.internal_index AS is_anchor,
                        st.balance_start
                    FROM account_bank_statement_line st_line
                    JOIN account_move move ON move.statement_line_id = st_line.id
                    LEFT JOIN account_bank_statement st ON st.id = st_line.statement_id
                    WHERE
                        st_line.internal_index <= %s
                        AND move.journal_id = %s
                        {extra_clause}
                    ORDER BY st_line.internal_index
                """,
                [max_index, journal.id] + extra_params,
            )
            for st_line_id, amount, is_anchor, balance_start in self._cr.fetchall():
                if is_anchor:
                    current_running_balance = balance_start
                current_running_balance += amount
                if record_by_id.get(st_line_id):
                    record_by_id[st_line_id].running_balance = current_running_balance

    @api.depends('date', 'sequence')
    def _compute_internal_index(self):
        """
        Internal index is a field that holds the combination of the date, compliment of sequence and id of each line.
        Using this prevents us having a compound index, and extensive where clauses.
        Without this finding lines before current line (which we need for calculating the running balance)
        would need a query like this:
          date < current date OR (date = current date AND sequence > current date) or (
          date = current date AND sequence = current sequence AND id < current id)
        which needs to be repeated all over the code.
        This would be simply "internal index < current internal index" using this field.
        Also, we would need a compound index of date + sequence + id
        on the table which is not possible because date is not in this table (it is in the account move table)
        unless we use a sql view which is more complicated.
        """
        # ensure we are using correct value for reversing sequence in the index (2147483647)
        # NOTE: assert self._fields['sequence'].column_type[1] == 'int4'
        # if for any reason it changes (how unlikely), we need to update this code

        for st_line in self.filtered(lambda line: line._origin.id):
            st_line.internal_index = f'{st_line.date.strftime("%Y%m%d")}' \
                                      f'{MAXINT - st_line.sequence:0>10}' \
                                      f'{st_line._origin.id:0>10}'

    @api.depends('journal_id', 'currency_id', 'amount', 'foreign_currency_id', 'amount_currency',
                 'move_id.to_check',
                 'move_id.line_ids.account_id', 'move_id.line_ids.amount_currency',
                 'move_id.line_ids.amount_residual_currency', 'move_id.line_ids.currency_id',
                 'move_id.line_ids.matched_debit_ids', 'move_id.line_ids.matched_credit_ids')
    def _compute_is_reconciled(self):
        """ Compute the field indicating if the statement lines are already reconciled with something.
        This field is used for display purpose (e.g. display the 'cancel' button on the statement lines).
        Also computes the residual amount of the statement line.
        """
        for st_line in self:
            _liquidity_lines, suspense_lines, _other_lines = st_line._seek_for_lines()

            # Compute residual amount
            if st_line.to_check:
                st_line.amount_residual = -st_line.amount_currency if st_line.foreign_currency_id else -st_line.amount
            elif suspense_lines.account_id.reconcile:
                st_line.amount_residual = sum(suspense_lines.mapped('amount_residual_currency'))
            else:
                st_line.amount_residual = sum(suspense_lines.mapped('amount_currency'))

            # Compute is_reconciled
            if not st_line.id:
                # New record: The journal items are not yet there.
                st_line.is_reconciled = False
            elif suspense_lines:
                # In case of the statement line comes from an older version, it could have a residual amount of zero.
                st_line.is_reconciled = suspense_lines.currency_id.is_zero(st_line.amount_residual)
            elif st_line.currency_id.is_zero(st_line.amount):
                st_line.is_reconciled = True
            else:
                # The journal entry seems reconciled.
                st_line.is_reconciled = True

    @api.onchange('journal_id')
    def _onchange_journal_id(self):
        """
        Reset the statement line when the journal is changed. In some rare cases that journal is not in the context
        the journal_id field might be accessible to the user. In this cse we need to reset the statement_id field when
        the journal_id is changed.
        :return:
        """
        self.statement_id = self._get_default_statement(self.journal_id.id, self.date)

    # -------------------------------------------------------------------------
    # CONSTRAINT METHODS
    # -------------------------------------------------------------------------

    @api.constrains('amount', 'amount_currency', 'currency_id', 'foreign_currency_id', 'journal_id')
    def _check_amounts_currencies(self):
        """ Ensure the consistency the specified amounts and the currencies. """

        for st_line in self:
            if st_line.foreign_currency_id == st_line.currency_id:
                raise ValidationError(_("The foreign currency must be different than the journal one: %s",
                                        st_line.currency_id.name))
            if not st_line.foreign_currency_id and st_line.amount_currency:
                raise ValidationError(_("You can't provide an amount in foreign currency without "
                                        "specifying a foreign currency."))
            if not st_line.amount_currency and st_line.foreign_currency_id:
                raise ValidationError(_("You can't provide a foreign currency without specifying an amount in "
                                        "'Amount in Currency' field."))

    # -------------------------------------------------------------------------
    # LOW-LEVEL METHODS
    # -------------------------------------------------------------------------

    def new(self, values=None, origin=None, ref=None):
        st_line = super().new(values, origin, ref)
        if not st_line.journal_id:  # might not be computed because declared by inheritance
            st_line.move_id._compute_journal_id()
        return st_line

    @api.model_create_multi
    def create(self, vals_list):
        # OVERRIDE
        counterpart_account_ids = []

        for vals in vals_list:
            if 'statement_id' in vals and 'journal_id' not in vals:
                statement = self.env['account.bank.statement'].browse(vals['statement_id'])
                # Ensure the journal is the same as the statement one.
                # journal_id is a required field in the view, so it should be always available if the user
                # is creating the record, however, if a sync/import modules tries to add a line to an existing
                # statement they can omit the journal field because it can be obtained from the statement
                if statement.journal_id:
                    vals['journal_id'] = statement.journal_id.id

            # Force the move_type to avoid inconsistency with residual 'default_move_type' inside the context.
            vals['move_type'] = 'entry'

            # Hack to force different account instead of the suspense account.
            counterpart_account_ids.append(vals.pop('counterpart_account_id', None))

        st_lines = super().create(vals_list)

        for i, st_line in enumerate(st_lines):
            counterpart_account_id = counterpart_account_ids[i]

            to_write = {'statement_line_id': st_line.id, 'narration': st_line.narration}
            if 'line_ids' not in vals_list[i]:
                to_write['line_ids'] = [(0, 0, line_vals) for line_vals in st_line._prepare_move_line_default_vals(
                    counterpart_account_id=counterpart_account_id)]

            st_line.move_id.write(to_write)

            # Otherwise field narration will be recomputed silently (at next flush) when writing on partner_id
            self.env.remove_to_compute(st_line.move_id._fields['narration'], st_line.move_id)

        # No need for the user to manage their status (from 'Draft' to 'Posted')
        st_lines.move_id.action_post()
        return st_lines

    def write(self, vals):
        # OVERRIDE

        res = super().write(vals)
        self._synchronize_to_moves(set(vals.keys()))
        return res

    def unlink(self):
        # OVERRIDE to unlink the inherited account.move (move_id field) as well.
        moves = self.with_context(force_delete=True).mapped('move_id')
        res = super().unlink()
        moves.unlink()
        return res

    @api.model
    def read_group(self, domain, fields, groupby, offset=0, limit=None, orderby=False, lazy=True):
        # Add latest running_balance in the read_group
        result = super(AccountBankStatementLine, self).read_group(
            domain, fields, groupby, offset=offset,
            limit=limit, orderby=orderby, lazy=lazy)
        show_running_balance = False
        # We loop over the content of groupby because the groupby date is in the form of "date:granularity"
        for el in groupby:
            if (el == 'statement_id' or el == 'journal_id' or el.startswith('date')) and 'running_balance' in fields:
                show_running_balance = True
                break
        if show_running_balance:
            for group_line in result:
                group_line['running_balance'] = self.search(group_line.get('__domain'), limit=1).running_balance or 0.0
        return result

    # -------------------------------------------------------------------------
    # ACTION METHODS
    # -------------------------------------------------------------------------

    def action_undo_reconciliation(self):
        """ Undo the reconciliation made on the statement line and reset their journal items
        to their original states.
        """
        self.line_ids.remove_move_reconcile()
        self.payment_ids.unlink()

        for st_line in self:
            st_line.with_context(force_delete=True).write({
                'to_check': False,
                'line_ids': [Command.clear()] + [
                    Command.create(line_vals) for line_vals in st_line._prepare_move_line_default_vals()],
            })

    # -------------------------------------------------------------------------
    # HELPERS
    # -------------------------------------------------------------------------

    def _find_or_create_bank_account(self):
        bank_account = self.env['res.partner.bank'].search([
            ('acc_number', '=', self.account_number),
            ('partner_id', '=', self.partner_id.id),
        ])
        if not bank_account:
            bank_account = self.env['res.partner.bank'].create({
                'acc_number': self.account_number,
                'partner_id': self.partner_id.id,
            })
        return bank_account

    def _get_amounts_with_currencies(self):
        """
        Returns the line amount in company, journal and foreign currencies
        """
        self.ensure_one()

        company_currency = self.journal_id.company_id.currency_id
        journal_currency = self.journal_id.currency_id or company_currency
        foreign_currency = self.foreign_currency_id or journal_currency or company_currency

        journal_amount = self.amount
        if foreign_currency == journal_currency:
            transaction_amount = journal_amount
        else:
            transaction_amount = self.amount_currency
        if journal_currency == company_currency:
            company_amount = journal_amount
        elif foreign_currency == company_currency:
            company_amount = transaction_amount
        else:
            company_amount = journal_currency._convert(journal_amount, company_currency,
                                                       self.journal_id.company_id, self.date)
        return company_amount, company_currency, journal_amount, journal_currency, transaction_amount, foreign_currency

    def _get_default_amls_matching_domain(self):
        return [
            # Base domain.
            ('display_type', 'not in', ('line_section', 'line_note')),
            ('parent_state', '=', 'posted'),
            ('company_id', '=', self.company_id.id),
            # Reconciliation domain.
            ('reconciled', '=', False),
            ('account_id.reconcile', '=', True),
            # Special domain for payments.
            '|',
            ('account_id.account_type', 'not in', ('asset_receivable', 'liability_payable')),
            ('payment_id', '=', False),
            # Special domain for statement lines.
            ('statement_line_id', '!=', self.id),
        ]

    @api.model
    def _get_default_journal(self):
        journal_type = self.env.context.get('journal_type', 'bank')
        return self.env['account.journal'].search([
                ('type', '=', journal_type),
                ('company_id', '=', self.env.company.id)
            ], limit=1)

    @api.model
    def _get_default_statement(self, journal_id=None, date=None):
        statement = self.search(
            domain=[
                ('journal_id', '=', journal_id or self._get_default_journal().id),
                ('date', '<=', date or fields.Date.today()),
            ],
            limit=1
        ).statement_id
        if not statement.is_complete:
            return statement

    def _prepare_counterpart_amounts_using_st_line_rate(self, currency, balance, amount_currency):
        """ Convert the amounts passed as parameters to the statement line currency using the rates provided by the
        bank. The computed amounts are the one that could be set on the statement line as a counterpart journal item
        to fully paid the provided amounts as parameters.

        :param currency:        The currency in which is expressed 'amount_currency'.
        :param balance:         The amount expressed in company currency. Only needed when the currency passed as
                                parameter is neither the statement line's foreign currency, neither the journal's
                                currency.
        :param amount_currency: The amount expressed in the 'currency' passed as parameter.
        :return:                A python dictionary containing:
            * balance:          The amount to consider expressed in company's currency.
            * amount_currency:  The amount to consider expressed in statement line's foreign currency.
        """
        self.ensure_one()
        company_amount, company_currency, journal_amount, journal_currency, transaction_amount, foreign_currency \
            = self._get_amounts_with_currencies()

        rate_journal2foreign_curr = journal_amount and abs(transaction_amount) / abs(journal_amount)
        rate_comp2journal_curr = company_amount and abs(journal_amount) / abs(company_amount)

        if currency == foreign_currency:
            trans_amount_currency = amount_currency
            if rate_journal2foreign_curr:
                journ_amount_currency = journal_currency.round(trans_amount_currency / rate_journal2foreign_curr)
            else:
                journ_amount_currency = 0.0
            if rate_comp2journal_curr:
                new_balance = company_currency.round(journ_amount_currency / rate_comp2journal_curr)
            else:
                new_balance = 0.0
        elif currency == journal_currency:
            trans_amount_currency = foreign_currency.round(amount_currency * rate_journal2foreign_curr)
            if rate_comp2journal_curr:
                new_balance = company_currency.round(amount_currency / rate_comp2journal_curr)
            else:
                new_balance = 0.0
        else:
            journ_amount_currency = journal_currency.round(balance * rate_comp2journal_curr)
            trans_amount_currency = foreign_currency.round(journ_amount_currency * rate_journal2foreign_curr)
            new_balance = balance

        return {
            'amount_currency': trans_amount_currency,
            'balance': new_balance,
        }

    def _prepare_move_line_default_vals(self, counterpart_account_id=None):
        """ Prepare the dictionary to create the default account.move.lines for the current account.bank.statement.line
        record.
        :return: A list of python dictionary to be passed to the account.move.line's 'create' method.
        """
        self.ensure_one()

        if not counterpart_account_id:
            counterpart_account_id = self.journal_id.suspense_account_id.id

        if not counterpart_account_id:
            raise UserError(_(
                "You can't create a new statement line without a suspense account set on the %s journal.",
                self.journal_id.display_name,
            ))

        company_amount, _company_currency, journal_amount, journal_currency, transaction_amount, foreign_currency \
            = self._get_amounts_with_currencies()

        liquidity_line_vals = {
            'name': self.payment_ref,
            'move_id': self.move_id.id,
            'partner_id': self.partner_id.id,
            'account_id': self.journal_id.default_account_id.id,
            'currency_id': journal_currency.id,
            'amount_currency': journal_amount,
            'debit': company_amount > 0 and company_amount or 0.0,
            'credit': company_amount < 0 and -company_amount or 0.0,
        }

        # Create the counterpart line values.
        counterpart_line_vals = {
            'name': self.payment_ref,
            'account_id': counterpart_account_id,
            'move_id': self.move_id.id,
            'partner_id': self.partner_id.id,
            'currency_id': foreign_currency.id,
            'amount_currency': -transaction_amount,
            'debit': -company_amount if company_amount < 0.0 else 0.0,
            'credit': company_amount if company_amount > 0.0 else 0.0,
        }
        return [liquidity_line_vals, counterpart_line_vals]

    def _seek_for_lines(self):
        """ Helper used to dispatch the journal items between:
        - The lines using the liquidity account.
        - The lines using the transfer account.
        - The lines being not in one of the two previous categories.
        :return: (liquidity_lines, suspense_lines, other_lines)
        """
        liquidity_lines = self.env['account.move.line']
        suspense_lines = self.env['account.move.line']
        other_lines = self.env['account.move.line']

        for line in self.move_id.line_ids:
            if line.account_id == self.journal_id.default_account_id:
                liquidity_lines += line
            elif line.account_id == self.journal_id.suspense_account_id:
                suspense_lines += line
            else:
                other_lines += line
        return liquidity_lines, suspense_lines, other_lines

    # SYNCHRONIZATION account.bank.statement.line <-> account.move
    # -------------------------------------------------------------------------

    def _synchronize_from_moves(self, changed_fields):
        """ Update the account.bank.statement.line regarding its related account.move.
        Also, check both models are still consistent.
        :param changed_fields: A set containing all modified fields on account.move.
        """
        if self._context.get('skip_account_move_synchronization'):
            return

        for st_line in self.with_context(skip_account_move_synchronization=True):
            move = st_line.move_id
            move_vals_to_write = {}
            st_line_vals_to_write = {}

            if 'line_ids' in changed_fields:
                liquidity_lines, suspense_lines, _other_lines = st_line._seek_for_lines()
                company_currency = st_line.journal_id.company_id.currency_id
                journal_currency = st_line.journal_id.currency_id if st_line.journal_id.currency_id != company_currency\
                    else False

                if len(liquidity_lines) != 1:
                    raise UserError(_(
                        "The journal entry %s reached an invalid state regarding its related statement line.\n"
                        "To be consistent, the journal entry must always have exactly one journal item involving the "
                        "bank/cash account."
                    ) % st_line.move_id.display_name)

                st_line_vals_to_write.update({
                    'payment_ref': liquidity_lines.name,
                    'partner_id': liquidity_lines.partner_id.id,
                })

                # Update 'amount' according to the liquidity line.

                if journal_currency:
                    st_line_vals_to_write.update({
                        'amount': liquidity_lines.amount_currency,
                    })
                else:
                    st_line_vals_to_write.update({
                        'amount': liquidity_lines.balance,
                    })

                if len(suspense_lines) == 1:

                    if journal_currency and suspense_lines.currency_id == journal_currency:

                        # The suspense line is expressed in the journal's currency meaning the foreign currency
                        # set on the statement line is no longer needed.

                        st_line_vals_to_write.update({
                            'amount_currency': 0.0,
                            'foreign_currency_id': False,
                        })

                    elif not journal_currency and suspense_lines.currency_id == company_currency:

                        # Don't set a specific foreign currency on the statement line.

                        st_line_vals_to_write.update({
                            'amount_currency': 0.0,
                            'foreign_currency_id': False,
                        })

                    else:

                        # Update the statement line regarding the foreign currency of the suspense line.

                        st_line_vals_to_write.update({
                            'amount_currency': -suspense_lines.amount_currency,
                            'foreign_currency_id': suspense_lines.currency_id.id,
                        })

                move_vals_to_write.update({
                    'partner_id': liquidity_lines.partner_id.id,
                    'currency_id': (st_line.foreign_currency_id or journal_currency or company_currency).id,
                })

            move.write(move._cleanup_write_orm_values(move, move_vals_to_write))
            st_line.write(move._cleanup_write_orm_values(st_line, st_line_vals_to_write))

    def _synchronize_to_moves(self, changed_fields):
        """ Update the account.move regarding the modified account.bank.statement.line.
        :param changed_fields: A list containing all modified fields on account.bank.statement.line.
        """
        if self._context.get('skip_account_move_synchronization'):
            return

        if not any(field_name in changed_fields for field_name in (
            'payment_ref', 'amount', 'amount_currency',
            'foreign_currency_id', 'currency_id', 'partner_id',
        )):
            return

        for st_line in self.with_context(skip_account_move_synchronization=True):
            liquidity_lines, suspense_lines, other_lines = st_line._seek_for_lines()
            journal = st_line.journal_id
            company_currency = journal.company_id.currency_id
            journal_currency = journal.currency_id if journal.currency_id != company_currency else False

            line_vals_list = st_line._prepare_move_line_default_vals()
            line_ids_commands = [(1, liquidity_lines.id, line_vals_list[0])]

            if suspense_lines:
                line_ids_commands.append((1, suspense_lines.id, line_vals_list[1]))
            else:
                line_ids_commands.append((0, 0, line_vals_list[1]))

            for line in other_lines:
                line_ids_commands.append((2, line.id))

            st_line_vals = {
                'currency_id': (st_line.foreign_currency_id or journal_currency or company_currency).id,
                'line_ids': line_ids_commands,
            }
            if st_line.move_id.journal_id != journal:
                st_line_vals['journal_id'] = journal.id
            if st_line.move_id.partner_id != st_line.partner_id:
                st_line_vals['partner_id'] = st_line.partner_id.id
            st_line.move_id.write(st_line_vals)


# For optimization purpose, creating the reverse relation of m2o in _inherits saves
# a lot of SQL queries
class AccountMove(models.Model):
    _name = "account.move"
    _inherit = ['account.move']

    statement_line_ids = fields.One2many('account.bank.statement.line', 'move_id', string='Statements')
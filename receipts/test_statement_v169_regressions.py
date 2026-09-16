"""Dependency-free regressions for documented settlement and per-event display.

The parser/processing helpers are executed from their actual source. Only their
Django imports and persistence collaborators are substituted; these tests do not
claim to exercise Django's template engine, ORM, HTTP handlers or PostgreSQL.
Run: python -m unittest receipts.test_statement_v169_regressions -v
"""
from __future__ import annotations
import ast
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import sys
from types import ModuleType, SimpleNamespace as NS
import unittest
from receipts.statement_matching import (
    AmountOption, EvidenceComponent, StatementLine, DOC_RECEIPT, DOC_REFUND,
    ROLE_CHARGE, ROLE_REFUND, USAGE_MODE_CONSUME, USAGE_MODE_REFERENCE,
    evidence_amount_options, matching_amount_pair, component_relevant_to_statement,
    merchant_keys_compatible, deduplicate_components, reconcile_statement,
    format_evidence_calculation,
)
from receipts.receipt_component_identity import component_fingerprint, source_component_key

ROOT = Path(__file__).parent
KIND = NS(UNKNOWN='unknown', CHARGE='charge', REFUND='refund', INVOICE='invoice')

def load_parser():
    path = ROOT / 'ai_filename.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    tree.body = [node for node in tree.body if not (
        isinstance(node, ast.ImportFrom) and node.module in {'django.conf', 'models'})]
    module = ModuleType('receipthub_v169_parser_test')
    sys.modules[module.__name__] = module
    module.__dict__.update(settings=NS(), ReceiptFinancialDocumentKind=KIND,
        ReceiptFilenameStatus=NS(READY='ready', NEEDS_REVIEW='needs_review', FAILED='failed', SKIPPED='skipped'))
    exec(compile(tree, str(path), 'exec'), module.__dict__)
    return module

PARSER = load_parser()

def load_processing(*names, **collaborators):
    path = ROOT / 'statement_processing.py'
    original = ast.parse(path.read_text(encoding='utf-8'))
    nodes = [node for node in original.body if
             isinstance(node, ast.FunctionDef) and node.name in names]
    if len(nodes) != len(names):
        raise AssertionError('Requested helper not found in source')
    tree = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *nodes], type_ignores=[])
    namespace = dict(date=date, Decimal=Decimal, InvalidOperation=InvalidOperation,
        re=re, EvidenceComponent=EvidenceComponent, AmountOption=AmountOption,
        ROLE_CHARGE=ROLE_CHARGE, ROLE_REFUND=ROLE_REFUND,
        USAGE_MODE_CONSUME=USAGE_MODE_CONSUME, USAGE_MODE_REFERENCE=USAGE_MODE_REFERENCE,
        DATE_MATCH_TOLERANCE_DAYS=1, merchant_keys_compatible=merchant_keys_compatible,
        matching_amount_pair=matching_amount_pair, evidence_amount_options=evidence_amount_options,
        component_relevant_to_statement=component_relevant_to_statement,
        normalize_documented_amount_options=PARSER.normalize_documented_amount_options,
        component_fingerprint=component_fingerprint, source_component_key=source_component_key,
        **collaborators)
    exec(compile(ast.fix_missing_locations(tree), str(path), 'exec'), namespace)
    return NS(**namespace)


def text_receipt(merchant='Higgsfield Inc.', amount='19.00', settled='3,142', rate='165.3465', day='12', invoice='DOC-1'):
    return f'''Receipt
Invoice number {invoice}
Date paid August {day}, 2026
{merchant}
Bill to
Example company
${amount} paid on August {day}, 2026
Description Qty Unit price Amount
Pro
Subtotal ${amount}
Total ${amount}
Amount paid ${amount}
Payment history
Payment method Date Amount paid Receipt number
Visa - 7210 August {day}, 2026 ${amount} 2255-6602
Charged ¥{settled} using 1 USD = {rate} JPY
'''


def component(key='C', *, amount='19', settled='3142', merchant='HIGGSFIELD', when=date(2026,8,12), receipt_id=1, invoice='DOC-1', **kw):
    return EvidenceComponent(key=key, receipt_id=receipt_id, receipt_order=receipt_id,
        filename=key+'.pdf', merchant_key=merchant, signed_amount=Decimal(amount),
        currency='USD', event_date=when, document_kind=DOC_RECEIPT,
        invoice_number=invoice, payee=merchant,
        amount_options=(() if settled is None else (AmountOption(Decimal(settled), 'JPY', 'receipt_charged'),)), **kw)


def line(key='L', *, amount='3142', currency='JPY', merchant='HIGGSFIELD', when=date(2026,8,12)):
    return StatementLine(key=key, sequence=1, transaction_date=when, merchant_key=merchant,
                         amount_options=(AmountOption(Decimal(amount),currency,'statement'),))


class DocumentedCurrencyParserTests(unittest.TestCase):
    def test_higgsfield_has_one_payment_with_jpy_alternative(self):
        result=PARSER.extract_receipt_text_fallback(text_receipt())
        self.assertEqual(len(result.transaction_components),1)
        event=result.transaction_components[0]
        self.assertEqual((event['signed_amount'],event['currency']),('19.00','USD'))
        self.assertEqual(event['amount_options'][0]['amount'],'3142.00')
        self.assertEqual(event['amount_options'][0]['currency'],'JPY')

    def test_runway_name_and_documented_jpy_survive_split_invoice_label(self):
        raw=text_receipt('Runway AI, Inc. @runwayml','38.50','6,270','162.8571','21','DOC-2').replace('Invoice number DOC-2','Invoice number\nDOC-2')
        result=PARSER.extract_receipt_text_fallback(raw)
        self.assertEqual(result.service_label,'Runway')
        self.assertEqual(result.transaction_components[0]['invoice_number'],'DOC-2')
        self.assertEqual(result.transaction_components[0]['amount_options'][0]['amount'],'6270.00')

    def test_no_settlement_is_invented_from_usd(self):
        raw=text_receipt().split('Charged')[0]
        self.assertFalse(PARSER.extract_receipt_text_fallback(raw).transaction_components[0].get('amount_options'))

    def test_tax_conversion_is_not_total_charged(self):
        raw=text_receipt().split('Charged')[0]+'JCT amount in JPY: ¥3,142 using a conversion rate of 165.3465.'
        self.assertFalse(PARSER.extract_receipt_text_fallback(raw).transaction_components[0].get('amount_options'))

    def test_conversion_without_payment_history_is_rejected(self):
        raw=text_receipt().replace('Payment history','Tax information')
        self.assertFalse(PARSER.extract_receipt_text_fallback(raw).transaction_components[0].get('amount_options'))

    def test_multiple_payment_rows_do_not_share_document_wide_conversion(self):
        raw=text_receipt()+'Visa - 7210 August 13, 2026 $10.00 9999-9999\n'
        self.assertFalse(PARSER.extract_receipt_text_fallback(raw).transaction_components[0].get('amount_options'))

    def test_multiple_conversion_lines_are_ambiguous(self):
        raw=text_receipt()+'Charged ¥3,143 using 1 USD = 165.4000 JPY\n'
        self.assertFalse(PARSER.extract_receipt_text_fallback(raw).transaction_components[0].get('amount_options'))

    def test_payment_history_date_conflict_is_rejected(self):
        raw=text_receipt().replace('Visa - 7210 August 12','Visa - 7210 August 13')
        self.assertFalse(PARSER.extract_receipt_text_fallback(raw).transaction_components[0].get('amount_options'))

    def test_payment_history_amount_conflict_is_rejected(self):
        raw=text_receipt().replace('2026 $19.00 2255','2026 $20.00 2255')
        self.assertFalse(PARSER.extract_receipt_text_fallback(raw).transaction_components[0].get('amount_options'))

    def test_mismatching_metadata_cannot_override_literal_amount(self):
        option={'amount':'1','currency':'JPY','basis':'receipt_charged','source_text':'Charged ¥3,142 using 1 USD = 165.3465 JPY'}
        self.assertFalse(PARSER.normalize_documented_amount_options([option], original_currency='USD'))

    def test_wrong_source_currency_is_rejected(self):
        option={'amount':'3142','currency':'JPY','basis':'receipt_charged','source_text':'Charged ¥3,142 using 1 EUR = 165.3465 JPY'}
        self.assertFalse(PARSER.normalize_documented_amount_options([option], original_currency='USD'))

    def test_wrong_settlement_symbol_is_rejected(self):
        self.assertIsNone(PARSER._parse_charged_conversion('Charged $3,142 using 1 USD = 165.3465 JPY'))

    def test_model_currency_duplicates_merge_into_one_documented_event(self):
        fallback=PARSER.extract_receipt_text_fallback(text_receipt())
        usd=dict(fallback.transaction_components[0]); usd.pop('amount_options'); usd['component_key']='ai-usd'
        jpy={**usd,'component_key':'ai-jpy','signed_amount':'3142.00','currency':'JPY'}
        payload,_=PARSER.merge_payload_with_text_fallback({'transaction_components':[usd,jpy]}, fallback, service_context='Higgsfield')
        self.assertEqual(len(payload['transaction_components']),1)
        self.assertEqual(payload['transaction_components'][0]['currency'],'USD')
        self.assertEqual(payload['transaction_components'][0]['amount_options'][0]['amount'],'3142.00')

    def test_model_different_invoice_is_not_silently_merged(self):
        fallback=PARSER.extract_receipt_text_fallback(text_receipt())
        other={**fallback.transaction_components[0], 'invoice_number':'DIFFERENT', 'component_key':'other'}
        payload,_=PARSER.merge_payload_with_text_fallback({'transaction_components':[other]}, fallback, service_context='Higgsfield')
        self.assertEqual(len(payload['transaction_components']),2)

    def test_generic_product_pro_replaced_by_documented_runway(self):
        fallback=PARSER.extract_receipt_text_fallback(text_receipt('Runway AI, Inc.'))
        payload,_=PARSER.merge_payload_with_text_fallback({'service_label':'Pro'},fallback,service_context='Runway')
        self.assertEqual(payload['service_label'],'Runway')

    def test_normalization_retains_settlement_source(self):
        original=PARSER.extract_receipt_text_fallback(text_receipt()).transaction_components
        values=PARSER.normalize_transaction_components(original,fallback_kind='charge')
        self.assertEqual(values[0]['amount_options'][0]['source_text'],original[0]['amount_options'][0]['source_text'])
        self.assertEqual(values[0]['metadata_version'],3)


class SettlementMatchingTests(unittest.TestCase):
    def test_higgsfield_exact_jpy_match(self):
        result=reconcile_statement([line()],[component()])
        self.assertEqual(result.assignments['L'].component_keys,('C',))
        self.assertIn('Charged',result.assignments['L'].memo)

    def test_runway_exact_jpy_match(self):
        result=reconcile_statement([line(amount='6270',merchant='RUNWAY',when=date(2026,8,21))],
            [component(amount='38.50',settled='6270',merchant='RUNWAY',when=date(2026,8,21))])
        self.assertIn('L',result.assignments)

    def test_one_yen_difference_is_not_accepted(self):
        self.assertFalse(reconcile_statement([line(amount='3143')],[component()]).assignments)

    def test_missing_documented_settlement_is_not_accepted(self):
        self.assertFalse(reconcile_statement([line()],[component(settled=None)]).assignments)

    def test_wrong_merchant_does_not_match(self):
        self.assertFalse(reconcile_statement([line(merchant='RUNWAY')],[component()]).assignments)

    def test_wrong_date_does_not_match(self):
        self.assertFalse(reconcile_statement([line(when=date(2026,8,15))],[component()]).assignments)

    def test_native_currency_conflict_cannot_be_bypassed(self):
        target=replace(line(),amount_options=(AmountOption(Decimal('20'),'USD'),AmountOption(Decimal('3142'),'JPY')))
        self.assertFalse(reconcile_statement([target],[component()]).assignments)

    def test_one_event_cannot_pay_usd_and_jpy_lines(self):
        result=reconcile_statement([line('jpy'),line('usd',amount='19',currency='USD')],[component()])
        self.assertEqual(len(result.assignments),1)
        self.assertEqual(result.consumed_component_keys,{'C'})

    def test_reserved_event_is_not_reused_in_other_currency(self):
        result=reconcile_statement([line()],[component()],unavailable_component_keys={'C'})
        self.assertFalse(result.assignments)

    def test_reupload_is_deduplicated_before_currency_matching(self):
        a=component('A'); b=component('B',receipt_id=2)
        result=reconcile_statement([line('jpy'),line('usd',amount='19',currency='USD')],[a,b])
        self.assertEqual(len(result.assignments),1)

    def test_duplicate_documents_preserve_verified_settlement_option(self):
        a=component('A',settled=None); b=component('B',receipt_id=2)
        unique,_=deduplicate_components([a,b])
        self.assertEqual(len(unique),1)
        self.assertTrue(matching_amount_pair(line(),unique[0]))

    def test_conflicting_settlements_for_one_event_require_review(self):
        event=replace(component(),amount_options=(
            AmountOption(Decimal('3142'),'JPY','receipt_charged'),
            AmountOption(Decimal('3143'),'JPY','receipt_charged')))
        self.assertFalse(matching_amount_pair(line(),event))

    def test_refund_cannot_inherit_charge_conversion(self):
        refund=replace(component(),role=ROLE_REFUND,signed_amount=Decimal('-19'))
        self.assertEqual(len(evidence_amount_options(refund)),1)

    def test_unverified_estimated_rate_option_is_ignored(self):
        event=replace(component(),amount_options=(AmountOption(Decimal('3142'),'JPY','estimated_fx'),))
        self.assertFalse(matching_amount_pair(line(),event))

    def test_display_uses_arrow_not_false_cross_currency_equation(self):
        text=format_evidence_calculation([component()],AmountOption(Decimal('3142'),'JPY'))
        self.assertIn('→',text)
        self.assertIn('19 USD',text)
        self.assertIn('3142 JPY',text)
        self.assertIn('領収書に明記',text)


class MonthScopeAndUsageTests(unittest.TestCase):
    def setUp(self):
        self.original=component('old',amount='22',settled=None,merchant='ANTHROPIC',when=date(2026,6,29),invoice='LN81OSYJ-0002',fingerprint='original')
        self.refund=replace(self.original,key='refund',signed_amount=Decimal('-.64'),event_date=date(2026,7,28),role=ROLE_REFUND,document_kind=DOC_REFUND,fingerprint='refund')
        self.target=line(merchant='ANTHROPIC',amount='74.31',currency='USD',when=date(2026,8,6))
        self.receipt=NS(pk=1,display_filename='Refund.pdf',original_filename='Refund.pdf',
            submission=NS(user=NS(username='test@example.com')),service_display_name_snapshot='その他',
            ai_extracted_service_label='Claude',ai_extracted_payee='Anthropic, PBC')
        self.helpers=load_processing('_build_unmatched_receipt_snapshot',
            _unused_component_reason=lambda *a:{'reason':'test','reason_code':'test'})

    def snapshot(self,unused,history=None,month=date(2026,8,1),current=None,events=None,lines=None):
        return self.helpers._build_unmatched_receipt_snapshot(unused_components=unused,
            all_components=events or [self.original,self.refund],unresolved_receipts=[],
            lines=lines if lines is not None else [self.target],receipt_by_id={1:self.receipt},
            global_usage_history=history or {},global_reference_history={},
            current_component_usage=current or {},target_month=month)

    def test_consumed_july_refund_cannot_pull_june_charge_into_august(self):
        rows=self.snapshot([self.original],history={'refund':[{'statement_month':'2026-07','line_reference':'0465','usage_mode':'consume'}]})
        self.assertEqual(rows,[])

    def test_truly_pending_july_refund_remains_visible(self):
        rows=self.snapshot([self.refund],history={'original':[{'usage_mode':'consume'}]})
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['usage_state'],'partial')
        self.assertEqual(rows[0]['role'],'refund')
        self.assertEqual(rows[0]['unused_component_count'],1)

    def test_current_month_extra_receipt_is_not_hidden_when_all_lines_match(self):
        current=replace(self.original,event_date=date(2026,8,8))
        rows=self.snapshot([current],events=[current],lines=[])
        self.assertEqual(len(rows),1)

    def test_original_charge_stays_available_in_its_own_month(self):
        self.assertEqual(len(self.snapshot([self.original],month=date(2026,6,1),lines=[])),1)

    def test_historical_charge_can_explain_actual_previous_month_line(self):
        old=line(merchant='ANTHROPIC',amount='22',currency='USD',when=date(2026,6,29))
        self.assertEqual(len(self.snapshot([self.original],lines=[old])),1)

    def test_current_reference_does_not_override_past_consumption(self):
        rows=self.snapshot([self.refund],history={'original':[{'usage_mode':'consume','line_reference':'0382'}]},
                           current={'original':[{'usage_mode':'reference'}]})
        original=next(c for c in rows[0]['components'] if c['component_key']=='old')
        self.assertTrue(original['consumed'])
        self.assertFalse(original['available'])
        self.assertTrue(original['referenced'])
        self.assertEqual(len(original['used_in']),2)

    def test_same_invoice_original_in_refund_and_invoice_has_one_identity(self):
        common=dict(merchant_key='ANTHROPIC',payee='Anthropic, PBC',role='charge',signed_amount='22',currency='USD',invoice_number='LN81OSYJ-0002',event_date=date(2026,6,29))
        a=component_fingerprint(**common,transaction_id='LN81OSYJ-0002')
        b=component_fingerprint(**common,transaction_id='2657-9811-5339')
        self.assertEqual(a,b)

    def test_refund_and_original_remain_separate_events(self):
        common=dict(merchant_key='ANTHROPIC',payee='Anthropic, PBC',currency='USD',invoice_number='LN81OSYJ-0002')
        a=component_fingerprint(**common,role='charge',signed_amount='22',event_date=date(2026,6,29))
        b=component_fingerprint(**common,role='refund',signed_amount='-.64',event_date=date(2026,7,28))
        self.assertNotEqual(a,b)


class LegacyIdentityAndRefreshTests(unittest.TestCase):
    def helpers(self):
        def identity(receipt, **kwargs):
            kwargs['signed_amount'] = kwargs.pop('amount')
            kwargs['source_component_key'] = kwargs.pop('raw_key')
            return component_fingerprint(**kwargs, receipt_id=receipt.pk)
        return load_processing('_evidence_component_fingerprint', '_parse_decimal', '_parse_date',
            _known_merchant_key=lambda value: 'HIGGSFIELD' if 'higgsfield' in str(value).lower() else ('ANTHROPIC' if ('anthropic' in str(value).lower() or 'claude' in str(value).lower()) else ''),
            _enrich_receipt_financial_metadata=lambda receipt: None,
            _ensure_receipt_file_sha256=lambda receipt: '',
            _component_fingerprint_for_receipt=identity)

    def evidence(self, *, currency='USD', amount='19', invoice='DOC-1'):
        fallback=PARSER.extract_receipt_text_fallback(text_receipt())
        receipt=NS(pk=1, financial_transaction_components=list(fallback.transaction_components))
        return NS(receipt=receipt,receipt_id=1,role='charge',signed_amount=Decimal(amount),
            currency=currency,event_date=date(2026,8,12),service_label_snapshot='Higgsfield',
            payee_snapshot='Higgsfield Inc.',invoice_number_snapshot=invoice,
            transaction_reference_snapshot='',related_transaction_reference_snapshot='',
            component_key='receipt-1:legacy',component_fingerprint='old-nonempty-fingerprint')

    def test_nonempty_legacy_fingerprint_is_upgraded_from_same_invoice(self):
        value=self.helpers()._evidence_component_fingerprint(self.evidence())
        expected=component_fingerprint(merchant_key='HIGGSFIELD',role='charge',signed_amount='19',currency='USD',invoice_number='DOC-1')
        self.assertEqual(value,expected)

    def test_legacy_jpy_evidence_shares_usd_event_identity(self):
        helpers=self.helpers()
        usd=helpers._evidence_component_fingerprint(self.evidence())
        jpy=helpers._evidence_component_fingerprint(self.evidence(currency='JPY',amount='3142'))
        self.assertEqual(usd,jpy)

    def test_other_invoice_is_not_reassigned_to_literal_invoice(self):
        helpers=self.helpers()
        self.assertNotEqual(helpers._evidence_component_fingerprint(self.evidence()),
                            helpers._evidence_component_fingerprint(self.evidence(invoice='OTHER')))

    def test_deleted_file_without_new_identity_keeps_old_consumption_lock(self):
        e=self.evidence(invoice='');e.receipt=None;e.receipt_id=None
        self.assertEqual(self.helpers()._evidence_component_fingerprint(e),'old-nonempty-fingerprint')

    def test_old_checked_receipt_is_reparsed_without_ai_and_cached(self):
        from io import BytesIO
        writes=[];reads=[]
        class Objects:
            def filter(self, **kwargs): return self
            def update(self, **kwargs): writes.append(kwargs)
        class File:
            name='proof.pdf'
            def open(self, *args): reads.append(True); return BytesIO(b'fixture')
        receipt=NS(pk=1,financial_metadata_checked_at=datetime.now(timezone.utc),
            plan_change_metadata_checked_at=datetime.now(timezone.utc),
            financial_transaction_components=[{'currency':'USD','signed_amount':'19'}],
            file_available=True,file=File(),original_filename='proof.pdf',content_type='application/pdf',
            financial_document_kind='charge',ai_extracted_payee='Higgsfield Inc.',ai_extracted_service_label='',
            ai_extracted_plan_name='',issued_on=date(2026,8,12),amount=Decimal('19'),currency='USD')
        helpers=load_processing('_enrich_receipt_financial_metadata',Path=Path,
            timezone=NS(now=lambda:datetime.now(timezone.utc)),logger=NS(exception=lambda *a:None),
            Receipt=NS(objects=Objects()),ReceiptFinancialDocumentKind=KIND,
            FINANCIAL_METADATA_VERSION=PARSER.FINANCIAL_METADATA_VERSION,
            extract_embedded_pdf_text=lambda **kwargs:text_receipt(),
            extract_receipt_text_fallback=PARSER.extract_receipt_text_fallback)
        helpers._enrich_receipt_financial_metadata(receipt)
        self.assertEqual(len(reads),1)
        self.assertEqual(receipt.financial_transaction_components[0]['amount_options'][0]['amount'],'3142.00')
        self.assertEqual(receipt.financial_transaction_components[0]['metadata_version'],3)
        helpers._enrich_receipt_financial_metadata(receipt)
        self.assertEqual(len(reads),1)
        self.assertEqual(len(writes),1)

    def test_evidence_snapshot_stores_one_primary_amount_and_settlement_note(self):
        enum=NS(CONSUME='consume',REFERENCE='reference')
        helpers=load_processing('_create_evidence_records','_target_amount_option',
            CardStatementReceiptEvidence=lambda **kwargs:NS(**kwargs),
            StatementReceiptEvidenceUsageMode=enum,StatementReceiptEvidenceRole=NS(CHARGE='charge',REFUND='refund'),
            _statement_line=lambda *args:line())
        records=helpers._create_evidence_records(NS(),[component()],{1:NS(pk=1)})
        self.assertEqual(len(records),1)
        self.assertEqual((records[0].signed_amount,records[0].currency),(Decimal('19'),'USD'))
        self.assertEqual(records[0].usage_mode,'consume')
        self.assertIn('3142 JPY',records[0].source_label)


class LedgerLockContractTests(unittest.TestCase):
    def test_postgres_uses_transaction_scoped_lock(self):
        calls=[]
        class Cursor:
            def __enter__(self): return self
            def __exit__(self,*args): return False
            def execute(self,*args): calls.append(args)
        helper=load_processing('_lock_component_ledger_for_reconciliation',
            connection=NS(vendor='postgresql',cursor=Cursor))
        helper._lock_component_ledger_for_reconciliation()
        self.assertEqual(calls,[("SELECT pg_advisory_xact_lock(%s)",[0x524850434C454447])])

    def test_sqlite_never_receives_postgres_sql(self):
        helper=load_processing('_lock_component_ledger_for_reconciliation',connection=NS(vendor='sqlite'))
        helper._lock_component_ledger_for_reconciliation()

    def test_ledger_lock_is_first_inside_transaction(self):
        tree=ast.parse((ROOT/'statement_processing.py').read_text())
        function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='reconcile_card_statement_items')
        block=next(n for n in ast.walk(function) if isinstance(n,ast.With))
        self.assertIsInstance(block.body[0],ast.Expr)
        self.assertEqual(block.body[0].value.func.id,'_lock_component_ledger_for_reconciliation')


if __name__=='__main__':
    unittest.main()

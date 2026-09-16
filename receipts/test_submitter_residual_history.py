"""Contact hint regressions; the pure engine and source-loaded ORM adapter.

The adapter tests execute the actual function bodies with in-memory query
collaborators, not Django/PostgreSQL. They verify control flow and payloads,
but do not claim HTTP/SQL integration coverage.
"""
from __future__ import annotations
import ast
from copy import deepcopy
from dataclasses import replace, asdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS
import unittest

from receipts.plan_change_matching import (
    HistoricalSubmitterUsage as Usage, PlanAmountOption as Money,
    PlanStatementLine as Line, suggest_previous_month_submitters as suggest,
    resolve_submitted_recurring_history as resolve, SUBMITTER_HINT_VERSION,
)
from receipts.statement_matching import EvidenceComponent, StatementLine, AmountOption, reconcile_statement
from receipts.test_statement_v169_regressions import load_processing, PARSER

MONTH = date(2026, 8, 1)

def hist(user=1, day=13, amount='4500', currency='JPY', source='K-JUL', service=1, **kw):
    return Usage(user_id=user, user_label=f'user-{user}', service_id=service,
        service_label='Suno', merchant_key='SUNO', event_date=date(2026,7,day),
        amount=Decimal(amount), currency=currency, receipt_id=user,
        filename=source+'.pdf', source_key=source,
        billing_type=kw.pop('billing_type','subscription'), **kw)

def current(h, day=None, source='K-AUG', receipt_id=50, confirmed=True, ref='0495', **kw):
    return replace(h, event_date=date(2026,8,day or h.event_date.day), document_event_date=None,
        receipt_id=receipt_id, source_key=source, filename=source+'.pdf',
        confirmed_statement_id=8 if confirmed else None,
        confirmed_line_key=ref if confirmed else '', confirmed_line_reference=ref if confirmed else '', **kw)

def target(amount='10', currency='USD', day=11, key='0492', merchant='SUNO'):
    return Line(key=key, transaction_date=date(2026,8,day), merchant_key=merchant,
        amount_options=(Money(Decimal(amount),currency),))

class ResidualHistoryEngineTests(unittest.TestCase):
    def test_suno_kim_renewal_is_excluded_and_matsuzaki_is_retained(self):
        kim=hist(previously_matched=True)
        ken=hist(2,11,'10','USD',source='M-JUL',service=2,previously_matched=True,
                 historical_statement_id=7,historical_line_reference='0416')
        result=suggest(target(),[kim,ken],current_charges=[current(kim)])
        self.assertEqual([r['user_id'] for r in result['candidates']],[2])
        self.assertEqual(result['candidates'][0]['historical_line_reference'],'0416')
        self.assertEqual(result['explained_history_count'],1)
        self.assertEqual(result['explained_history'][0]['current_matches'][0]['line_reference'],'0495')
        self.assertFalse(result['consumes_receipts'])

    def test_unknown_owner_does_not_fallback_to_only_available_wrong_price(self):
        h=hist()
        result=suggest(target(),[h],current_charges=[current(h)])
        self.assertEqual(result['candidates'],[])

    def test_wrong_price_remains_excluded_without_any_current_match(self):
        line=replace(target(),amount_options=(Money(10,'USD'),Money(1655,'JPY')))
        self.assertEqual(suggest(line,[hist()])['candidates'],[])

    def test_same_price_already_covered_is_also_excluded(self):
        h=hist(amount='10',currency='USD')
        self.assertEqual(suggest(target(),[h],current_charges=[current(h)])['candidates'],[])

    def test_matching_different_users_receipt_does_not_exclude_history(self):
        h=hist(amount='10',currency='USD')
        c=current(replace(h,user_id=2))
        self.assertEqual(len(suggest(target(),[h],current_charges=[c])['candidates']),1)

    def test_matching_other_contract_keeps_users_independent_missing_contract(self):
        h1=hist(source='A',contract_key='contract-a')
        h2=hist(day=11,amount='10',currency='USD',source='B',contract_key='contract-b')
        r=suggest(target(),[h1,h2],current_charges=[current(h1)])
        self.assertEqual([c['historical_filename'] for c in r['candidates']],['B.pdf'])

    def test_other_service_id_does_not_resolve_same_merchant_contract(self):
        h=hist(amount='10',currency='USD')
        c=current(replace(h,service_id=2))
        self.assertEqual(len(suggest(target(),[h],current_charges=[c])['candidates']),1)

    def test_explicit_other_subscription_id_does_not_cover_history(self):
        h=hist(amount='10',currency='USD',contract_key='a')
        c=current(replace(h,contract_key='b'))
        self.assertEqual(len(suggest(target(),[h],current_charges=[c])['candidates']),1)

    def test_other_card_does_not_cover_same_amount(self):
        h=hist(amount='10',currency='USD',card_last4='7210')
        c=current(replace(h,card_last4='0100'))
        self.assertEqual(len(suggest(target(),[h],current_charges=[c])['candidates']),1)

    def test_unconfirmed_current_receipt_is_review_not_covered(self):
        h=hist(amount='10',currency='USD',day=11)
        c=current(h,confirmed=False)
        r=suggest(target(),[h],current_charges=[c])
        self.assertEqual(r['candidates'][0]['submission_state'],'uploaded_review')
        self.assertEqual(r['explained_history_count'],0)

    def test_already_allocated_current_document_not_shown_as_unparsed_missing(self):
        h=hist(amount='10',currency='USD',billing_type='metered')
        r=suggest(target(),[h],current_charges=[current(h)])
        self.assertEqual(r['candidates'][0]['current_receipts'],[])

    def test_other_cycle_does_not_cover_this_contract(self):
        h=hist(amount='10',currency='USD',day=11)
        c=current(h,day=25)
        self.assertEqual(len(suggest(target(),[h],current_charges=[c])['candidates']),1)

    def test_history_reuploads_and_card_date_aliases_do_not_leave_residual(self):
        h=hist(amount='10',currency='USD')
        dup=replace(h,receipt_id=99,filename='renamed.pdf')
        card=replace(h,event_date=date(2026,7,14),document_event_date=h.event_date)
        r=suggest(target(),[h,dup,card],current_charges=[current(h)])
        self.assertEqual(r['candidates'],[])
        self.assertEqual(r['explained_history_count'],1)

    def test_one_current_event_cannot_cover_two_previous_distinct_events(self):
        a=hist(amount='10',currency='USD',source='one')
        b=replace(a,source_key='two',receipt_id=99)
        covered=resolve([a,b],[current(a)],month=MONTH)
        self.assertEqual(len(covered),0) # the individual pairing is ambiguous
        self.assertEqual(len(suggest(target(),[a,b],current_charges=[current(a)])['candidates']),1)

    def test_current_reupload_cannot_cover_two_distinct_contracts(self):
        a=hist(source='one'); b=replace(a,source_key='two')
        c=current(a); dup=replace(c,receipt_id=99,filename='duplicate.pdf')
        self.assertEqual(resolve([a,b],[c,dup],month=MONTH),{})

    def test_two_confirmed_events_can_cover_two_same_price_contracts(self):
        a=hist(source='one'); b=replace(a,source_key='two')
        c=current(a); d=replace(c,source_key='other-current',confirmed_line_key='0500',confirmed_line_reference='0500')
        covered=resolve([a,b],[c,d],month=MONTH)
        self.assertEqual(len(covered),2)
        self.assertTrue(all(x['group_covered'] for x in covered.values()))

    def test_exact_date_renewal_wins_over_neighbor_history(self):
        a=hist(day=11,source='one'); b=hist(day=13,source='two')
        covered=resolve([a,b],[current(b)],month=MONTH)
        self.assertEqual([x['historical_filename'] for x in covered.values()],['two.pdf'])

    def test_month_end_is_calendar_based(self):
        h=replace(hist(),event_date=date(2028,1,31))
        c=replace(current(hist()),event_date=date(2028,2,29))
        self.assertEqual(len(resolve([h],[c],month=date(2028,2,1))),1)

    def test_prior_and_current_are_never_mutated(self):
        h=hist(); c=current(h)
        before=asdict(h),asdict(c)
        suggest(target(),[h],current_charges=[c])
        self.assertEqual(before,(asdict(h),asdict(c)))

    def test_output_is_json_serializable(self):
        import json
        h=hist(); ken=hist(2,11,'10','USD',source='m',service=2)
        json.dumps(suggest(target(),[h,ken],current_charges=[current(h)]))

    def test_documented_settlement_amount_is_used_without_exchange_guess(self):
        h=hist(amount='19',currency='USD',amount_options=(Money(3142,'JPY'),))
        r=suggest(target('3142','JPY',13),[h])
        self.assertEqual(len(r['candidates']),1)
        self.assertEqual(suggest(target('3000','JPY',13),[h])['candidates'],[])

    def test_documented_alias_can_resolve_renewal(self):
        h=hist(amount='19',currency='USD',amount_options=(Money(3142,'JPY'),))
        c=current(replace(h,amount=Decimal(3142),currency='JPY',amount_options=()))
        self.assertEqual(len(resolve([h],[c],month=MONTH)),1)

    def test_wrong_cadence_not_in_candidates_at_same_price(self):
        self.assertEqual(suggest(target(),[hist(day=25,amount='10',currency='USD')])['candidates'],[])

    def test_one_time_purchase_is_not_a_recurring_candidate(self):
        self.assertEqual(suggest(target(),[hist(day=11,amount='10',currency='USD',billing_type='one_time')])['candidates'],[])

    def test_metered_purchases_not_blanket_suppressed_by_current_charge(self):
        h=hist(day=11,amount='10',currency='USD',billing_type='metered')
        r=suggest(target(),[h],current_charges=[current(h)])
        self.assertEqual(len(r['candidates']),1)
        self.assertEqual(r['candidates'][0]['support_level'],1)

    def test_one_history_closer_unmatched_line_excludes_farther_line(self):
        h=hist(day=11,amount='10',currency='USD')
        close=target(key='close'); far=target(day=13,key='far')
        self.assertEqual(suggest(far,[h],target_lines=[far,close])['candidates'],[])
        self.assertEqual(len(suggest(close,[h],target_lines=[far,close])['candidates']),1)

    def test_equally_compatible_lines_are_alternatives_not_two_missing_receipts(self):
        h=hist(day=11,amount='10',currency='USD')
        a=target(key='a'); b=target(key='b')
        c=suggest(a,[h],target_lines=[a,b])['candidates'][0]
        self.assertTrue(c['ambiguous'])
        self.assertEqual(c['competing_line_keys'],['b'])
        self.assertTrue(any('複数件の未提出' in text for text in c['reasons']))

    def test_history_order_does_not_change_candidate(self):
        h=hist(amount='10',currency='USD',day=11)
        h2=hist(2,11,'10','USD',source='m',service=2)
        self.assertEqual(suggest(target(),[h,h2]),suggest(target(),[h2,h]))

    def test_wrong_vendor_never_explains_history(self):
        h=hist(amount='10',currency='USD')
        c=replace(current(h),merchant_key='GITHUB')
        self.assertEqual(resolve([h],[c],month=MONTH),{})

    def test_negative_charge_cannot_explain_positive_renewal(self):
        h=hist(); c=replace(current(h),amount=Decimal('-4500'))
        self.assertEqual(resolve([h],[c],month=MONTH),{})

    def test_reference_hints_do_not_turn_unmatched_charge_into_a_match(self):
        # Run the actual monetary engine around hint generation.
        h=hist(); ken=hist(2,11,'10','USD',source='m',service=2)
        lines=[StatementLine(key='0492',sequence=1,transaction_date=date(2026,8,11),
                merchant_key='SUNO',amount_options=(AmountOption(10,'USD','statement'),)),
               StatementLine(key='0495',sequence=2,transaction_date=date(2026,8,13),
                merchant_key='SUNO',amount_options=(AmountOption(4500,'JPY','statement'),))]
        evidence=[EvidenceComponent(key='one',receipt_id=50,receipt_order=1,filename='current.pdf',
                    merchant_key='SUNO',signed_amount=Decimal(4500),currency='JPY',event_date=date(2026,8,13))]
        before=reconcile_statement(lines,evidence)
        suggest(target(),[h,ken],current_charges=[current(h)])
        after=reconcile_statement(lines,evidence)
        self.assertEqual(set(before.assignments),{'0495'})
        self.assertEqual(before.assignments,after.assignments)


STATUS=NS(MATCHED='matched',UNMATCHED='unmatched',NEEDS_REVIEW='needs_review')
REASON=NS(AUTO_STRONG='auto_strong',MANUAL_CONFIRMED='manual_confirmed',ORIGINAL_CHARGE='original_charge')
KIND=NS(REFUND='refund',CHARGE='charge',INVOICE='invoice')

def user(pk):
    return NS(pk=pk,get_full_name=lambda:'',get_username=lambda:f'user-{pk}')

def receipt(pk,who,day,amount,currency='JPY',month=7,invoice='',raw=True):
    service=NS(pk=who,billing_type='subscription')
    charge={'component_key':'primary','role':'charge','signed_amount':str(amount),'currency':currency,
            'transaction_date':date(2026,month,day).isoformat(),'service_label':'Suno','payee':'Suno Inc.',
            'invoice_number':invoice or f'I-{pk}','transaction_id':f'R-{pk}'}
    return NS(pk=pk,submission=NS(user=user(who),user_id=who,period_month=date(2026,month+1,1)),
        service_id=who,service=service,ai_extracted_card_last4='7210',
        financial_document_kind='charge',financial_transaction_components=[charge] if raw else [],
        amount=Decimal(str(amount)) if raw else None,currency=currency if raw else '',
        issued_on=date(2026,month,day) if raw else None,ai_extracted_service_label='Suno',
        ai_extracted_payee='Suno Inc.',financial_transaction_reference=invoice or f'I-{pk}',
        file_sha256='',billing_type_snapshot='subscription',display_filename=f'{pk}.pdf',
        service_display_name_snapshot='Suno',file_available=True)

def item(pk,day,amount,currency='JPY',status='unmatched',month=8):
    statement=NS(pk=month,period_month=date(2026,month,1),submission_month=date(2026,month+1,1),card_last4='7210')
    return NS(pk=pk,sequence=pk,line_reference=f'{pk:04}',transaction_date=date(2026,month,day),
        receipt_required=True,match_status=status,match_reason_code='auto_strong' if status=='matched' else 'none',
        statement=statement,statement_id=month,matched_user=None,matched_service=None,
        amount=Decimal(str(amount)),currency=currency,submitter_candidates={'version':1,'candidates':[{'user_id':999}]})

def evidence(r,it,amount=None,currency=None,day=None):
    return NS(receipt=r,receipt_id=r.pk if r else None,statement_item=it,statement_item_id=it.pk,
        component_key='primary',component_fingerprint='',role='charge',usage_mode='consume',
        signed_amount=Decimal(str(amount if amount is not None else r.amount)),
        currency=currency or r.currency,event_date=day or r.issued_on,
        invoice_number_snapshot=r.financial_transaction_reference if r else 'retained-invoice',
        transaction_reference_snapshot=f'R-{r.pk}' if r else '',filename_snapshot=f'{r.pk}.pdf' if r else 'retained.pdf',
        payee_snapshot='Suno Inc.',service_label_snapshot='Suno')

class Query(list):
    def select_related(self,*args):return self
    def values_list(self,*args,**kwargs):return self

class Manager:
    def __init__(self,values):self.values=values;self.calls=[]
    def filter(self,**kw):
        self.calls.append(kw)
        if 'statement_item_id__in' in kw:
            return Query([v for v in self.values if v.statement_item_id in kw['statement_item_id__in']])
        if 'statement_item__statement__period_month__lt' in kw:
            return Query([v for v in self.values if v.statement_item.statement.period_month < kw['statement_item__statement__period_month__lt']])
        return Query(self.values)


def adapter(evidences=(), active=(1,2), service_ids=(1,2), gate=()):
    manager=Manager(list(evidences))
    def parse_decimal(x):
        try:return Decimal(str(x))
        except:return None
    def parse_date(x):
        if isinstance(x,date):return x
        try:return date.fromisoformat(x)
        except:return None
    def add_months(d,n):
        yy,mm=divmod(d.year*12+d.month-1+n,12)
        return date(yy,mm+1,1)
    helpers=load_processing('_submitter_review_notes','_submitter_usage_rows','_submitter_rows_from_confirmed_evidence',
        '_merge_submitter_history_rows','_confirm_current_submitter_rows','_attach_previous_month_submitter_candidates',
        HistoricalSubmitterUsage=Usage,PlanAmountOption=Money,replace=replace,
        _parse_decimal=parse_decimal,_parse_date=parse_date,
        _known_merchant_key=lambda name:'SUNO' if 'suno' in name.lower() else '',
        _canonical_merchant_key=lambda name,catalogs:'SUNO' if 'suno' in name.lower() else '',
        settings=NS(RECEIPT_CARD_LAST4='7210'),ReceiptFinancialDocumentKind=KIND,
        StatementMatchStatus=STATUS,StatementMatchReason=REASON,
        StatementReceiptEvidenceRole=NS(CHARGE='charge'),StatementReceiptEvidenceUsageMode=NS(CONSUME='consume'),
        _statement_item_is_reversal=lambda it:getattr(it,'is_reversal',False),
        _statement_gate_errors=lambda stmt:list(gate),
        _registered_services_for_period=lambda period:[NS(pk=k) for k in service_ids],
        get_user_model=lambda:NS(objects=Manager(list(active))),UserAccountStatus=NS(ACTIVE='active'),
        CardStatementReceiptEvidence=NS(objects=manager),add_months=add_months,
        _statement_line=lambda it,cats:Line(str(it.pk),it.transaction_date,'SUNO',(Money(it.amount,it.currency),)),
        _plan_statement_line=lambda value:value,
        suggest_previous_month_submitters=suggest,resolve_submitted_recurring_history=resolve,
        timezone=NS(now=lambda:datetime(2026,9,17,tzinfo=timezone.utc)))
    helpers.manager=manager
    return helpers

class AdapterFlowTests(unittest.TestCase):
    def test_actual_adapter_uses_unsaved_current_run_matches(self):
        a=adapter(); kim=receipt(1,1,13,4500); ken=receipt(2,2,11,10,'USD'); cur=receipt(50,1,13,4500,month=8)
        missing=item(492,11,10,'USD'); matched=item(495,13,4500,status='matched')
        c=evidence(cur,matched)
        a._attach_previous_month_submitter_candidates(missing.statement,[missing,matched],[cur],[kim,ken],[],
            evidence_components_by_item={495:[c]},evidence_usage_mode_by_item={495:'consume'})
        self.assertEqual([x['user_id'] for x in missing.submitter_candidates['candidates']],[2])
        self.assertEqual(missing.submitter_candidates['explained_history_count'],1)
        self.assertEqual(matched.submitter_candidates,{})
        self.assertEqual(missing.match_status,'unmatched')
        self.assertEqual(matched.match_status,'matched')

    def test_matched_same_amount_is_not_a_candidate_when_ledger_is_not_yet_saved(self):
        a=adapter(); h=receipt(1,1,11,10,'USD'); c=receipt(50,1,11,10,'USD',month=8)
        missing=item(492,11,10,'USD'); matched=item(495,11,10,'USD',status='matched')
        a._attach_previous_month_submitter_candidates(missing.statement,[missing,matched],[c],[h],[],
            evidence_components_by_item={495:[evidence(c,matched)]})
        self.assertEqual(missing.submitter_candidates['candidates'],[])

    def test_manual_persisted_evidence_is_also_seen(self):
        h=receipt(1,1,11,10,'USD'); c=receipt(50,1,11,10,'USD',month=8)
        missing=item(492,11,10,'USD'); matched=item(495,11,10,'USD',status='matched')
        a=adapter([evidence(c,matched)])
        a._attach_previous_month_submitter_candidates(missing.statement,[missing,matched],[c],[h],[])
        self.assertEqual(missing.submitter_candidates['candidates'],[])

    def test_only_the_consumed_charge_in_a_multi_charge_pdf_is_confirmed(self):
        a=adapter(); r=receipt(50,1,13,4500,month=8); it=item(495,13,4500,status='matched')
        rows=[current(hist(),confirmed=False),current(hist(amount='10',currency='USD'),confirmed=False,source='other')]
        out=a._confirm_current_submitter_rows(rows,[(evidence(r,it),it,'consume')])
        self.assertTrue(out[0].confirmed_line_key)
        self.assertFalse(out[1].confirmed_line_key)

    def test_reference_only_charge_does_not_resolve_history(self):
        a=adapter(); r=receipt(50,1,13,4500,month=8); it=item(495,13,4500,status='matched')
        out=a._confirm_current_submitter_rows([current(hist(),confirmed=False)],[(evidence(r,it),it,'reference')])
        self.assertFalse(out[0].confirmed_line_key)

    def test_refund_component_does_not_confirm_charge(self):
        a=adapter(); r=receipt(50,1,13,4500,month=8); it=item(495,13,4500,status='matched')
        e=evidence(r,it);e.role='refund'
        out=a._confirm_current_submitter_rows([current(hist(),confirmed=False)],[(e,it,'consume')])
        self.assertFalse(out[0].confirmed_line_key)

    def test_same_pdf_different_date_not_marked_confirmed(self):
        a=adapter(); r=receipt(50,1,14,4500,month=8); it=item(495,14,4500,status='matched')
        out=a._confirm_current_submitter_rows([current(hist(),confirmed=False)],[(evidence(r,it),it,'consume')])
        self.assertFalse(out[0].confirmed_line_key)

    def test_previous_confirmed_evidence_outside_storage_pool_is_recovered(self):
        r=receipt(2,2,11,10,'USD'); old=item(416,11,10,'USD',status='matched',month=7)
        a=adapter([evidence(r,old)]); missing=item(492,11,10,'USD')
        a._attach_previous_month_submitter_candidates(missing.statement,[missing],[],[],[])
        self.assertEqual(missing.submitter_candidates['candidates'][0]['user_id'],2)
        self.assertEqual(missing.submitter_candidates['candidates'][0]['historical_line_reference'],'0416')
        self.assertNotIn('receipt_id__in',a.manager.calls[0])

    def test_expired_previous_file_keeps_confirmed_reference(self):
        r=receipt(2,2,11,10,'USD');r.file_available=False
        old=item(416,11,10,'USD',status='matched',month=7);a=adapter([evidence(r,old)])
        missing=item(492,11,10,'USD')
        a._attach_previous_month_submitter_candidates(missing.statement,[missing],[],[],[])
        self.assertEqual(missing.submitter_candidates['candidates'][0]['user_id'],2)

    def test_missing_raw_metadata_is_recovered_from_evidence_snapshot(self):
        r=receipt(2,2,11,10,'USD',raw=False);old=item(416,11,10,'USD',status='matched',month=7)
        e=evidence(r,old,10,'USD',date(2026,7,11));a=adapter([e]);missing=item(492,11,10,'USD')
        a._attach_previous_month_submitter_candidates(missing.statement,[missing],[],[r],[])
        self.assertEqual(missing.submitter_candidates['candidates'][0]['user_id'],2)

    def test_deleted_receipt_with_direct_confirmed_owner_is_recoverable(self):
        old=item(416,11,10,'USD',status='matched',month=7);old.matched_user=user(2)
        a=adapter([evidence(None,old,10,'USD',date(2026,7,11))]);missing=item(492,11,10,'USD')
        a._attach_previous_month_submitter_candidates(missing.statement,[missing],[],[],[])
        self.assertEqual(missing.submitter_candidates['candidates'][0]['user_id'],2)

    def test_deleted_receipt_without_confirmed_owner_does_not_invent_one(self):
        old=item(416,11,10,'USD',status='matched',month=7)
        a=adapter([evidence(None,old,10,'USD',date(2026,7,11))]);missing=item(492,11,10,'USD')
        a._attach_previous_month_submitter_candidates(missing.statement,[missing],[],[],[])
        self.assertEqual(missing.submitter_candidates['candidates'],[])

    def test_net_group_primary_user_not_assumed_owner_of_missing_receipt(self):
        old=item(416,11,10,'USD',status='matched',month=7);old.matched_user=user(2);old.match_reason_code='merchant_refund_net'
        a=adapter([evidence(None,old,10,'USD',date(2026,7,11))]);missing=item(492,11,10,'USD')
        a._attach_previous_month_submitter_candidates(missing.statement,[missing],[],[],[])
        self.assertEqual(missing.submitter_candidates['candidates'],[])

    def test_unknown_owner_multi_charge_aggregate_not_reused(self):
        old=item(416,11,20,'USD',status='matched',month=7);old.matched_user=user(2)
        e=evidence(None,old,10,'USD',date(2026,7,11));a=adapter([e,deepcopy(e)]);missing=item(492,11,10,'USD')
        a._attach_previous_month_submitter_candidates(missing.statement,[missing],[],[],[])
        self.assertEqual(missing.submitter_candidates['candidates'],[])

    def test_other_card_previous_evidence_is_excluded(self):
        old=item(416,11,10,'USD',status='matched',month=7);old.statement.card_last4='0100'
        r=receipt(2,2,11,10,'USD');a=adapter([evidence(r,old)]);missing=item(492,11,10,'USD')
        a._attach_previous_month_submitter_candidates(missing.statement,[missing],[],[],[])
        self.assertEqual(missing.submitter_candidates['candidates'],[])

    def test_inactive_user_history_is_retained_as_review_only(self):
        a=adapter(active=(1,));h=receipt(2,2,11,10,'USD');missing=item(492,11,10,'USD')
        h.submission.user.is_active=False
        a._attach_previous_month_submitter_candidates(missing.statement,[missing],[],[h],[])
        candidate=missing.submitter_candidates['candidates'][0]
        self.assertEqual(candidate['user_id'],2)
        self.assertTrue(candidate['reference_only'])
        self.assertIn('停止中',candidate['review_notes'][0])
        self.assertEqual(missing.match_status,'unmatched')

    def test_wrong_price_current_document_not_described_as_unparsed(self):
        a=adapter();h=receipt(1,1,11,10,'USD');c=receipt(50,1,13,4500,month=8);missing=item(492,11,10,'USD')
        a._attach_previous_month_submitter_candidates(missing.statement,[missing],[c],[h],[])
        self.assertEqual(missing.submitter_candidates['candidates'][0]['current_receipts'],[])

    def test_actually_unparsed_current_document_is_review_not_missing_accusation(self):
        a=adapter();h=receipt(1,1,11,10,'USD');c=receipt(50,1,11,10,'USD',month=8,raw=False);missing=item(492,11,10,'USD')
        a._attach_previous_month_submitter_candidates(missing.statement,[missing],[c],[h],[])
        self.assertEqual(missing.submitter_candidates['candidates'][0]['submission_state'],'uploaded_review')

    def test_statement_validation_error_clears_hints(self):
        a=adapter(gate=('wrong-month',));missing=item(492,11,10,'USD')
        a._attach_previous_month_submitter_candidates(missing.statement,[missing],[],[receipt(1,1,11,10,'USD')],[])
        self.assertEqual(missing.submitter_candidates,{})

    def test_reversal_never_receives_purchase_owner_candidates(self):
        a=adapter();missing=item(492,11,10,'USD');missing.is_reversal=True
        a._attach_previous_month_submitter_candidates(missing.statement,[missing],[],[receipt(1,1,11,10,'USD')],[])
        self.assertEqual(missing.submitter_candidates,{})

    def test_metadata_card_date_aliases_share_one_identity(self):
        a=adapter();raw=hist(amount='10',currency='USD',day=11)
        ev=replace(raw,source_key='legacy-key',historical_statement_id=7,event_date=date(2026,7,12),document_event_date=raw.event_date)
        rows=a._merge_submitter_history_rows([raw],[ev])
        self.assertEqual({r.source_key for r in rows},{raw.source_key})
        self.assertEqual(len(resolve(rows,[current(raw)],month=MONTH)),1)

    def test_different_user_reupload_does_not_inherit_confirmation(self):
        a=adapter(); r=receipt(50,1,13,4500,month=8); it=item(495,13,4500,status='matched')
        e=evidence(r,it);e.component_fingerprint='shared-event'
        first=current(hist(),confirmed=False,source='shared-event')
        other=replace(first,user_id=2,receipt_id=51)
        out=a._confirm_current_submitter_rows([first,other],[(e,it,'consume')])
        self.assertTrue(out[0].confirmed_line_key)
        self.assertFalse(out[1].confirmed_line_key)

    def test_same_user_reupload_inherits_financial_event_confirmation(self):
        a=adapter(); r=receipt(50,1,13,4500,month=8); it=item(495,13,4500,status='matched')
        e=evidence(r,it);e.component_fingerprint='shared-event'
        first=current(hist(),confirmed=False,source='shared-event')
        other=replace(first,receipt_id=51)
        out=a._confirm_current_submitter_rows([first,other],[(e,it,'consume')])
        self.assertTrue(all(row.confirmed_line_key for row in out))

    def test_old_receipt_with_two_charges_only_strong_for_confirmed_component(self):
        a=adapter(); first=hist(amount='10',currency='USD')
        other=replace(first,amount=Decimal(20),source_key='other-event')
        ev=replace(first,previously_matched=True,historical_statement_id=7)
        rows=a._merge_submitter_history_rows([first,other],[ev])
        self.assertTrue(all(row.previously_matched for row in rows if row.amount == 10))
        self.assertTrue(all(not row.previously_matched for row in rows if row.amount == 20))

    def test_old_hint_guard_in_model_blocks_stale_candidates(self):
        path=Path(__file__).with_name('models.py')
        tree=ast.parse(path.read_text())
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='CardStatementItem')
        fn=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='current_submitter_candidates')
        fn.decorator_list=[]
        module=ast.Module(body=[fn],type_ignores=[])
        env={'__package__':'receipts','StatementMatchStatus':STATUS}
        exec(compile(ast.fix_missing_locations(module),str(path),'exec'),env)
        fn=env['current_submitter_candidates'];ob=item(492,11,10,'USD')
        self.assertTrue(fn(ob)['stale'])
        ob.submitter_candidates={'version':SUBMITTER_HINT_VERSION,'candidates':[{'user_id':2}]}
        self.assertEqual(fn(ob)['candidates'],[{'user_id':2}])
        ob.match_status='matched'
        self.assertEqual(fn(ob),{})

    def test_ui_and_pdf_use_version_guard_and_no_raw_hint_access(self):
        root=Path(__file__).parent.parent
        html=(root/'templates/receipts/_staff_card_statement_item.html').read_text()
        pdf=(root/'receipts/statement_pdf.py').read_text()
        self.assertIn('item.current_submitter_candidates',html)
        self.assertNotIn('item.submitter_candidates',html)
        self.assertIn('item.current_submitter_candidates',pdf)
        self.assertNotIn('item.submitter_candidates',pdf)

    def test_main_reconcile_passes_new_in_memory_components(self):
        source=Path(__file__).with_name('statement_processing.py').read_text()
        tree=ast.parse(source)
        main=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_reconcile_card_statement_items_impl')
        calls=[n for n in ast.walk(main) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name)
               and n.func.id=='_attach_previous_month_submitter_candidates']
        self.assertEqual(len(calls),1)
        self.assertIn('evidence_components_by_item',{kw.arg for kw in calls[0].keywords})
        self.assertIn('evidence_usage_mode_by_item',{kw.arg for kw in calls[0].keywords})

if __name__=='__main__':
    unittest.main()

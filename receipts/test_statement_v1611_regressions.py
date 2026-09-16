"""v1.16.11: actual source/engine regressions; no Django/SQL claims.

Fixtures transcribe the user's July/August statement screenshots. Production
code contains no employee, amount, or line-number special cases.
"""
from __future__ import annotations
import ast
from copy import deepcopy
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from receipts.test_submitter_residual_history import (
    adapter, receipt, item, evidence, target, hist, current, Usage, Money, Line,
    suggest, resolve, MONTH,
)
from receipts.test_statement_v169_regressions import load_processing

ROOT = Path(__file__).parent

def merchant(name, *_):
    value = ''.join(c for c in name.upper() if c.isalnum())
    for key, pattern in [('GOOGLE_CLOUD','GOOGLECLOUD'),('GOOGLE_ONE','GOOGLEONE'),
                         ('GOOGLE_PLAY','GOOGLEPLAY'),('SUNO','SUNO')]:
        if pattern in value:
            return key
    return ''

def source_receipt(pk, who, day, amount, currency, service, *, month=7, active=True):
    r = receipt(pk,who,day,amount,currency,month=month)
    r.submission.user.is_active=active
    r.submission.user.get_username=lambda: {1:'keeseon.kim',2:'ken.matsuzaki',3:'takahiro.motohashi'}[who]
    r.service.pk=who*10+{'Google One':1,'Suno':2,'Google Cloud':3}[service]; r.service_id=r.service.pk
    r.service.is_active=active
    r.service.billing_type='metered' if service=='Google Cloud' else 'subscription'
    r.billing_type_snapshot=r.service.billing_type
    r.service_display_name_snapshot=r.ai_extracted_service_label=service
    r.ai_extracted_payee=service+' Inc.'
    for raw in r.financial_transaction_components:
        raw['service_label']=service;raw['payee']=r.ai_extracted_payee
    r.display_filename=f'{str(month).zfill(2)}_{r.submission.user.get_username()}_{service}_{amount}_{currency}.pdf'
    return r

def source_evidence(r,old):
    e=evidence(r,old)
    e.service_label_snapshot=r.ai_extracted_service_label
    e.payee_snapshot=r.ai_extracted_payee
    e.filename_snapshot=r.display_filename
    return e

def source_line(pk,day,amount,currency,service,**kw):
    it=item(pk,day,amount,currency,**kw)
    it.merchant_key=merchant(service)
    return it

class SuppliedThreeLineTests(unittest.TestCase):
    def fixture(self, *, source_inactive=True):
        rows=[
            (415,2,11,10,32000,'JPY','Google One'),
            (416,2,11,11,10,'USD','Suno'),
            (432,3,20,20,2000,'JPY','Google Cloud'),
            (445,1,23,24,32000,'JPY','Google One'),
            (428,1,13,13,4500,'JPY','Suno'),
            (389,3,1,1,43,'JPY','Google Cloud'),
        ]
        allocations=[];saved=[]
        for pk,who,docday,cardday,amount,cur,service in rows:
            r=source_receipt(pk,who,docday,amount,cur,service,active=(who==1 or not source_inactive))
            old=source_line(pk,cardday,amount,cur,service,status='matched',month=7)
            allocations.append(source_evidence(r,old));saved.append(r)
        missing=[source_line(491,10,32000,'JPY','Google Play'),
                 source_line(492,11,10,'USD','Suno'),
                 source_line(512,19,2000,'JPY','Google Cloud')]
        a=adapter(allocations,active=(1,),service_ids=(1,))
        env=a._attach_previous_month_submitter_candidates.__globals__
        env['_known_merchant_key']=merchant
        env['_canonical_merchant_key']=merchant
        env['_statement_line']=lambda it,cats: Line(str(it.pk),it.transaction_date,it.merchant_key,(Money(it.amount,it.currency),))
        cur1=source_receipt(521,1,23,32000,'JPY','Google One',month=8)
        cur2=source_receipt(495,1,13,4500,'JPY','Suno',month=8)
        matched=[source_line(521,23,32000,'JPY','Google Play',status='matched'),
                 source_line(495,13,4500,'JPY','Suno',status='matched')]
        return a,missing,matched,[cur1,cur2],saved

    def run_case(self,*,source_inactive=True,raw_history=False):
        a,missing,matched,current_rows,previous=self.fixture(source_inactive=source_inactive)
        before=[it.match_status for it in missing+matched]
        # Evidence-only past source exercises PDFs stored outside the expected
        # submission cycle, or past metadata that is no longer parseable.
        a._attach_previous_month_submitter_candidates(missing[0].statement,missing+matched,current_rows,
            previous if raw_history else [],[],
            evidence_components_by_item={it.pk:[source_evidence(r,it)] for it,r in zip(matched,current_rows)})
        self.assertEqual([it.match_status for it in missing+matched],before)
        return missing

    def test_three_lines_are_recovered_from_confirmed_history_not_current_registration(self):
        result=self.run_case()
        self.assertEqual([[c['user_label'] for c in it.submitter_candidates['candidates']] for it in result],
                         [['ken.matsuzaki'],['ken.matsuzaki'],['takahiro.motohashi']])
        self.assertEqual([it.submitter_candidates['candidates'][0]['historical_line_reference'] for it in result],
                         ['0415','0416','0432'])
        self.assertEqual([it.submitter_candidates['candidates'][0]['date_distance'] for it in result],[0,0,1])
        self.assertEqual(len({c['user_id'] for it in result for c in it.submitter_candidates['candidates']}),2)

    def test_three_lines_with_active_accounts_have_same_answer(self):
        result=self.run_case(source_inactive=False)
        self.assertEqual([it.submitter_candidates['candidates'][0]['user_id'] for it in result],[2,2,3])

    def test_metadata_and_card_date_aliases_do_not_duplicate_candidates(self):
        for it in self.run_case(raw_history=True):
            self.assertEqual(it.submitter_candidates['total_candidates'],1)

    def test_kim_only_excluded_for_explained_matching_contract(self):
        result=self.run_case()
        self.assertEqual(result[0].submitter_candidates['explained_history'][0]['current_matches'][0]['line_reference'],'0521')
        self.assertEqual(result[1].submitter_candidates['explained_history'][0]['current_matches'][0]['line_reference'],'0495')

    def test_no_auto_match_no_financial_consumption(self):
        for it in self.run_case():
            hint=it.submitter_candidates
            self.assertTrue(hint['suggestion_only'])
            self.assertFalse(hint['consumes_receipts'])
            self.assertTrue(hint['candidates'][0]['reference_only'])
            self.assertEqual(it.match_status,'unmatched')

    def test_inactive_history_includes_review_note_not_silent_exclusion(self):
        for it in self.run_case():
            self.assertTrue(it.submitter_candidates['candidates'][0]['review_notes'])
            self.assertFalse(it.submitter_candidates['history_diagnostics']['current_registration_filter_applied'])

    def test_history_diagnostic_includes_prior_confirmed_card_rows(self):
        for it in self.run_case():
            self.assertEqual(it.submitter_candidates['history_diagnostics']['confirmed_source_lines'],6)

    def test_document_date_is_preserved_beside_card_date(self):
        c=self.run_case()[0].submitter_candidates['candidates'][0]
        self.assertEqual(c['historical_event_date'],'2026-07-10')
        self.assertEqual(c['historical_document_date'],'2026-07-11')

    def test_stop_flags_do_not_widen_amount_or_currency_rules(self):
        h=hist(previously_matched=True,review_notes=('stopped',))
        self.assertEqual(suggest(target(),[h])['candidates'],[])

    def test_equal_priced_two_users_remain_ambiguous(self):
        h=hist(day=11,amount='10',currency='USD',previously_matched=True)
        h2=replace(h,user_id=2,user_label='another-user',source_key='other')
        result=suggest(target(),[h,h2])
        self.assertEqual(len(result['candidates']),2)
        self.assertTrue(all(c['ambiguous'] for c in result['candidates']))

    def test_explicit_subscription_id_survives_service_reregistration(self):
        h=hist(day=11,amount='10',currency='USD',contract_key='sub-1')
        c=current(replace(h,service_id=1000))
        self.assertEqual(suggest(target(),[h],current_charges=[c])['candidates'],[])

    def test_api_prior_match_is_contact_evidence_not_subscription_proof(self):
        result=self.run_case()[2].submitter_candidates['candidates'][0]
        self.assertGreaterEqual(result['support_level'],2)
        self.assertIn('定期契約とは断定せず',' '.join(result['reasons']))


def load_views(*names,**collaborators):
    path=ROOT/'views.py';tree=ast.parse(path.read_text())
    nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
    for n in nodes:n.decorator_list=[]
    env={'__name__':'source_view_test',**collaborators}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),*nodes],type_ignores=[])),str(path),'exec'),env)
    return NS(**env)

class DisplayReadPathTests(unittest.TestCase):
    def test_get_handlers_cannot_reconcile_or_infer_or_parse(self):
        tree=ast.parse((ROOT/'views.py').read_text())
        names={'staff_card_statements','staff_card_statement_status','load_statement_result_display'}
        forbidden={'reconcile_pending_card_statement_month_semantics','reconcile_card_statement_items',
            'refresh_statement_submitter_candidates','_attach_previous_month_submitter_candidates',
            'extract_embedded_pdf_text','generate_card_statement_analysis'}
        for fn in [n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]:
            calls={n.func.id for n in ast.walk(fn) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name)}
            self.assertFalse(calls & forbidden,(fn.name,calls & forbidden))

    def test_display_loader_only_prefetches_visible_rows(self):
        items=[NS(pk=i,display=False) for i in range(67)]
        stmt=NS(ai_admin_memo='',items=items)
        recorded=[]
        class Query:
            def filter(self,*a,**kw):return self
            def annotate(self,*a,**kw):return self
            def order_by(self,*a,**kw):return self
            def prefetch_related(self,*a,**kw):return self
            def select_related(self,*a,**kw):return self
            def only(self,*a,**kw):recorded.append(('columns',a));return self
            def __iter__(self):return iter([stmt])
        q=Query();model=NS(objects=q,_meta=NS(concrete_fields=[]))
        def prepare(stmts,filt):
            stmt.display_inferred_items=[];stmt.display_unmatched_items=items[:3]
            stmt.display_items=items if filt=='all' else []
            return {'all':67,'unmatched':3}
        f=load_views('load_statement_result_display',CardStatement=model,CardStatementItem=model,
            CardStatementReceiptEvidence=model,CardStatementPlanChangeInference=model,
            Exists=lambda x:x,OuterRef=lambda x:x,Prefetch=lambda *a,**kw:(a,kw),
            prepare_statement_result_display=prepare,statement_reconciliation_pending=lambda s:False,
            prefetch_related_objects=lambda rows,*args:recorded.append(('rows',len(rows))))
        stmts,counts=f.load_statement_result_display(MONTH,'unmatched')
        self.assertIn(('rows',3),recorded)
        self.assertEqual(counts,{'all':67,'unmatched':3})
        self.assertFalse(any('financial_transaction_components' in str(row) for row in recorded))
        recorded.clear();f.load_statement_result_display(MONTH,'all')
        self.assertIn(('rows',67),recorded)

    def test_candidate_only_refresh_updates_only_json_not_financial_ledger(self):
        tree=ast.parse((ROOT/'statement_processing.py').read_text())
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='refresh_statement_submitter_candidates')
        calls=[n for n in ast.walk(fn) if isinstance(n,ast.Call)]
        forbidden={'_enrich_receipt_financial_metadata','_receipt_components','_backfill_missing_evidence_fingerprints',
                   'reconcile_statement','reconcile_card_statement_items','generate_card_statement_analysis'}
        self.assertFalse({n.func.id for n in calls if isinstance(n.func,ast.Name)} & forbidden)
        updates=[n for n in calls if isinstance(n.func,ast.Attribute) and n.func.attr=='bulk_update']
        self.assertEqual(ast.literal_eval(updates[0].args[1]),['submitter_candidates'])

    def test_explicit_reconcile_still_settles_cross_month_pending_ownership(self):
        tree=ast.parse((ROOT/'views.py').read_text())
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='staff_reconcile_card_statement')
        calls={n.func.id for n in ast.walk(fn) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name)}
        self.assertIn('reconcile_pending_card_statement_month_semantics',calls)
        self.assertIn('reconcile_card_statement_items',calls)

    def test_candidate_refresh_is_staff_only_post_and_has_url(self):
        tree=ast.parse((ROOT/'views.py').read_text())
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='staff_refresh_statement_candidates')
        self.assertEqual({ast.unparse(n) for n in fn.decorator_list},{'staff_member_required','require_POST'})
        self.assertIn('views.staff_refresh_statement_candidates',(ROOT/'urls.py').read_text())

    def test_js_supports_abort_latest_response_and_browser_back(self):
        source=(ROOT.parent/'static/js/staff_statement_processing.js').read_text()
        for expected in ['AbortController','serial !== requestSerial','popstate','pushState',
                         'cache: "no-store"','payload.result_filter','data-statement-filter-link']:
            self.assertIn(expected,source)

    def test_summary_evidence_presence_does_not_issue_per_row_queries(self):
        tree=ast.parse((ROOT/'models.py').read_text())
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='CardStatementItem')
        fn=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='evidence_count')
        fn.decorator_list=[];env={}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),'models.py','exec'),env)
        self.assertEqual(env['evidence_count'](NS(_display_has_evidence=True)),1)
        self.assertEqual(env['evidence_count'](NS(_display_has_evidence=False)),0)

if __name__=='__main__':unittest.main()

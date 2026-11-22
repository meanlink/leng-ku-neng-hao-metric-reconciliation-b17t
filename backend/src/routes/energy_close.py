from fastapi import APIRouter, Depends

from ..closing_schemas import CorrectionDecision, CorrectionIn, SiteBudgetIn, TransferIn
from ..db import db_cursor, lock_period
from ..security import current_user, require_roles
from ..services.audit import record_action
from ..services.budgets import budget_sheet, report_by_id, report_period, save_site_budget
from ..services.corrections import correction_period, create_correction, decide_correction, reading_snapshot
from ..services.transfers import create_transfer, decide_transfer, transfer_periods

router = APIRouter(tags=["抄表更正与站点结算"])


@router.get('/reading-corrections')
def list_corrections(user=Depends(current_user)):
    with db_cursor() as cur:
        cur.execute("""SELECT c.*, r.period, r.reading_date, r.value AS original_value, r.unit,
                       r.throughput_ton_day AS original_ton_days, r.import_batch, s.source_code, s.site
                       FROM reading_corrections c JOIN meter_readings r ON r.id=c.reading_id
                       JOIN meter_sources s ON s.id=r.source_id ORDER BY (c.status='pending') DESC,c.id DESC""")
        return cur.fetchall()


@router.post('/reading-corrections', status_code=201)
def submit_correction(payload: CorrectionIn, user=Depends(require_roles('analyst', 'manager'))):
    with db_cursor() as cur:
        reading = reading_snapshot(cur, payload.reading_id)
    with db_cursor(commit=True) as cur:
        lock_period(cur, reading['period'])
        return create_correction(cur, payload, reading, user['sub'])


@router.post('/reading-corrections/{correction_id}/decision')
def review_correction(correction_id: int, payload: CorrectionDecision, user=Depends(require_roles('reviewer', 'manager'))):
    period = correction_period(correction_id)
    with db_cursor(commit=True) as cur:
        lock_period(cur, period)
        return decide_correction(cur, correction_id, payload, user['sub'])


@router.get('/reading-transfers')
def list_transfers(user=Depends(current_user)):
    with db_cursor() as cur:
        cur.execute("""SELECT t.*, r.reading_date, r.value, r.unit, r.throughput_ton_day, r.import_batch,
                       fs.source_code AS from_source_code, fs.site AS from_site,
                       ts.source_code AS to_source_code, ts.site AS to_site
                       FROM reading_transfers t JOIN meter_readings r ON r.id=t.reading_id
                       JOIN meter_sources fs ON fs.id=t.from_source_id JOIN meter_sources ts ON ts.id=t.to_source_id
                       ORDER BY (t.status='pending') DESC,t.id DESC""")
        return cur.fetchall()


@router.post('/reading-transfers', status_code=201)
def submit_transfer(payload: TransferIn, user=Depends(require_roles('analyst', 'manager'))):
    with db_cursor() as cur:
        reading = reading_snapshot(cur, payload.reading_id)
    with db_cursor(commit=True) as cur:
        for period in sorted({reading['period'], payload.target_period}):
            lock_period(cur, period)
        return create_transfer(cur, payload, reading, user['sub'])


@router.post('/reading-transfers/{transfer_id}/decision')
def review_transfer(transfer_id: int, payload: CorrectionDecision, user=Depends(require_roles('reviewer', 'manager'))):
    periods = transfer_periods(transfer_id)
    with db_cursor(commit=True) as cur:
        for period in sorted({periods['from_period'], periods['to_period']}):
            lock_period(cur, period)
        return decide_transfer(cur, transfer_id, payload, user['sub'])


@router.get('/report-budgets/{report_id}')
def get_budget_sheet(report_id: int, user=Depends(current_user)):
    with db_cursor(commit=True) as cur:
        return budget_sheet(cur, report_by_id(cur, report_id))


@router.put('/report-budgets/{report_id}')
def set_budget(report_id: int, payload: SiteBudgetIn, user=Depends(require_roles('analyst', 'manager'))):
    period = report_period(report_id)
    with db_cursor(commit=True) as cur:
        lock_period(cur, period)
        report = report_by_id(cur, report_id)
        save_site_budget(cur, report, payload, user['sub'])
        record_action(cur, user['sub'], 'save_site_energy_budget', 'monthly_report', report_id, payload.site)
        return budget_sheet(cur, report)

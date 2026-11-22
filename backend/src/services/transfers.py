from fastapi import HTTPException

from ..db import db_cursor
from .audit import record_action
from .corrections import recalculate_source_cases, reading_snapshot


def transfer_periods(transfer_id):
    with db_cursor() as cur:
        cur.execute("SELECT from_period, to_period FROM reading_transfers WHERE id=%s", (transfer_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, detail="归属转移单不存在")
        return row


def ensure_periods_open(cur, *periods):
    for period in sorted(set(periods)):
        cur.execute("SELECT id FROM monthly_reports WHERE period=%s AND status='published'", (period,))
        if cur.fetchone():
            raise HTTPException(409, detail=f"核算月 {period} 的月报已签发，归属转移整笔拒绝")


def create_transfer(cur, payload, reading, user_id):
    ensure_periods_open(cur, reading['period'], payload.target_period)
    if reading['source_id'] == payload.target_source_id and reading['period'] == payload.target_period:
        raise HTTPException(422, detail="转移目标的电表与核算月不能和原位置相同")
    cur.execute("SELECT * FROM meter_sources WHERE id=%s", (payload.target_source_id,))
    target = cur.fetchone()
    if not target:
        raise HTTPException(404, detail="目标电表来源不存在")
    if target['unit'] != reading['unit'] or target['cycle'] != reading['cycle']:
        raise HTTPException(422, detail="目标电表的单位或周期与抄表记录不兼容")
    cur.execute("SELECT id FROM reading_corrections WHERE active_reading_id=%s", (reading['id'],))
    if cur.fetchone():
        raise HTTPException(409, detail="该抄表记录已有待复核或已批准的更正，转移与更正仅生效一项")
    cur.execute("SELECT id FROM reading_transfers WHERE active_reading_id=%s", (reading['id'],))
    if cur.fetchone():
        raise HTTPException(409, detail="该抄表记录已有待复核或已批准的归属转移")
    cur.execute("""SELECT id FROM meter_readings WHERE source_id=%s AND period=%s AND reading_date=%s""",
                (payload.target_source_id, payload.target_period, reading['reading_date']))
    if cur.fetchone():
        raise HTTPException(409, detail="转入位置已有读数，不能发起归属转移")
    cur.execute("""INSERT INTO reading_transfers(reading_id,from_source_id,from_period,to_source_id,to_period,evidence,submitted_by)
                   VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                (reading['id'], reading['source_id'], reading['period'],
                 payload.target_source_id, payload.target_period, payload.evidence, user_id))
    row_id = cur.lastrowid
    record_action(cur, user_id, 'submit_reading_transfer', 'reading_transfer', row_id, payload.evidence)
    cur.execute("SELECT * FROM reading_transfers WHERE id=%s", (row_id,))
    return cur.fetchone()


def refresh_source_reading_cache(cur, source_id):
    cur.execute("""UPDATE meter_sources SET last_reading=COALESCE((
                       SELECT value FROM meter_readings WHERE source_id=%s
                       ORDER BY reading_date DESC, id DESC LIMIT 1), 0)
                   WHERE id=%s""", (source_id, source_id))


def decide_transfer(cur, row_id, payload, user_id):
    cur.execute("SELECT * FROM reading_transfers WHERE id=%s FOR UPDATE", (row_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(404, detail="归属转移单不存在")
    if row['status'] != 'pending':
        # 批准或退回超时后的重试：直接返回已生效的结果，不重复转移。
        if row['status'] == payload.decision:
            return row
        raise HTTPException(409, detail="只有待复核的转移单才能处理")
    ensure_periods_open(cur, row['from_period'], row['to_period'])
    if int(user_id) == row['submitted_by']:
        raise HTTPException(409, detail="转移单须由另一位人员复核")
    reading = reading_snapshot(cur, row['reading_id'])
    decision, note = payload.decision, payload.note
    if decision == 'approved':
        cur.execute("""SELECT id FROM meter_readings WHERE source_id=%s AND period=%s AND reading_date=%s AND id<>%s""",
                    (row['to_source_id'], row['to_period'], reading['reading_date'], reading['id']))
        if cur.fetchone():
            decision = 'rejected'
            note = f"转入位置已有读数，归属转移退回。{payload.note}"[:500]
    if decision == 'approved':
        cur.execute("UPDATE meter_readings SET source_id=%s, period=%s WHERE id=%s",
                    (row['to_source_id'], row['to_period'], reading['id']))
        refresh_source_reading_cache(cur, row['from_source_id'])
        refresh_source_reading_cache(cur, row['to_source_id'])
        reason = f"归属转移 #{row_id} 已生效，重新复核"
        recalculate_source_cases(cur, {'period': row['from_period'], 'source_id': row['from_source_id']}, reason)
        recalculate_source_cases(cur, {'period': row['to_period'], 'source_id': row['to_source_id']}, reason)
    cur.execute("""UPDATE reading_transfers SET status=%s, decided_by=%s, decided_at=NOW(), decision_note=%s WHERE id=%s""",
                (decision, user_id, note, row_id))
    record_action(cur, user_id, f'{decision}_reading_transfer', 'reading_transfer', row_id, note)
    cur.execute("SELECT * FROM reading_transfers WHERE id=%s", (row_id,))
    return cur.fetchone()

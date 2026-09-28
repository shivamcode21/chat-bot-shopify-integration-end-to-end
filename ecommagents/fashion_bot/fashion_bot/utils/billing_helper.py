"""
Billing Helper Functions

Utilities to calculate billable conversations and generate billing reports
using the conversation classification system.

Only billable conversations are counted:
- user_initiated: Customer starts conversation ✅
- template_converted: Customer replies to template ✅
- template_initiated: No customer response ❌
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, List
from fashion_bot.history.conversation_classification import (
    aget_billable_conversation_count,
    aget_template_stats
)
from fashion_bot.database_manager import get_async_postgres_connection

logger = logging.getLogger(__name__)


async def acalculate_monthly_billing(
    client_id: str,
    year: int,
    month: int
) -> Dict[str, Any]:
    """Calculate billing for a specific month (async)."""
    try:
        start_date = datetime(year, month, 1, tzinfo=timezone.utc)

        if month == 12:
            end_date = datetime(year + 1, 1, 1, tzinfo=timezone.utc) - timedelta(seconds=1)
        else:
            end_date = datetime(year, month + 1, 1, tzinfo=timezone.utc) - timedelta(seconds=1)

        logger.info(f"[BILLING] Calculating for {year}-{month:02d}")
        logger.info(f"[BILLING] Date range: {start_date} to {end_date}")

        billable_count = await aget_billable_conversation_count(client_id, start_date, end_date)
        template_stats = await aget_template_stats(client_id, start_date, end_date)
        type_breakdown = await _aget_conversation_type_breakdown(client_id, start_date, end_date)

        billing_data = {
            "client_id": client_id,
            "period": f"{year}-{month:02d}",
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "billable_conversations": billable_count,
            "conversation_breakdown": type_breakdown,
            "template_stats": template_stats,
            "calculated_at": datetime.now(timezone.utc).isoformat()
        }

        logger.info(f"[BILLING] ✅ Billing calculated: {billable_count} billable conversations")
        return billing_data

    except Exception as e:
        logger.error(f"[BILLING] ❌ Error calculating billing: {e}", exc_info=True)
        return {}


async def acalculate_current_month_billing(client_id: str) -> Dict[str, Any]:
    """Calculate billing for the current month (month-to-date) (async)."""
    now = datetime.now(timezone.utc)
    return await acalculate_monthly_billing(client_id, now.year, now.month)


async def aget_billing_summary_for_date_range(
    client_id: str,
    start_date: datetime,
    end_date: datetime
) -> Dict[str, Any]:
    """Get comprehensive billing summary for a custom date range (async)."""
    try:
        billable_count = await aget_billable_conversation_count(client_id, start_date, end_date)
        template_stats = await aget_template_stats(client_id, start_date, end_date)
        type_breakdown = await _aget_conversation_type_breakdown(client_id, start_date, end_date)
        daily_breakdown = await _aget_daily_conversation_breakdown(client_id, start_date, end_date)

        return {
            "client_id": client_id,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "total_billable_conversations": billable_count,
            "conversation_breakdown": type_breakdown,
            "template_stats": template_stats,
            "daily_breakdown": daily_breakdown,
            "calculated_at": datetime.now(timezone.utc).isoformat()
        }

    except Exception as e:
        logger.error(f"[BILLING] ❌ Error getting billing summary: {e}", exc_info=True)
        return {}


async def _aget_conversation_type_breakdown(
    client_id: str,
    start_date: datetime,
    end_date: datetime
) -> Dict[str, int]:
    """Get count of conversations by type (async)."""
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                sql = """
                    SELECT
                        conversation_type,
                        COUNT(*) as count
                    FROM conversations
                    WHERE client_id = %s
                    AND created_at >= %s
                    AND created_at <= %s
                    GROUP BY conversation_type;
                """

                await cur.execute(sql, (client_id, start_date, end_date))
                results = await cur.fetchall()

                breakdown = {
                    "user_initiated": 0,
                    "template_initiated": 0,
                    "template_converted": 0
                }

                for row in results:
                    conv_type, count = row
                    if conv_type in breakdown:
                        breakdown[conv_type] = count

                return breakdown

    except Exception as e:
        logger.error(f"[BILLING] Error getting type breakdown: {e}")
        return {}


async def _aget_daily_conversation_breakdown(
    client_id: str,
    start_date: datetime,
    end_date: datetime
) -> List[Dict[str, Any]]:
    """Get daily breakdown of conversations (async)."""
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                sql = """
                    SELECT
                        DATE(created_at) as day,
                        conversation_type,
                        COUNT(*) as count
                    FROM conversations
                    WHERE client_id = %s
                    AND created_at >= %s
                    AND created_at <= %s
                    GROUP BY DATE(created_at), conversation_type
                    ORDER BY day DESC;
                """

                await cur.execute(sql, (client_id, start_date, end_date))
                results = await cur.fetchall()

                daily_data = {}
                for row in results:
                    day, conv_type, count = row
                    day_str = day.isoformat() if day else "unknown"

                    if day_str not in daily_data:
                        daily_data[day_str] = {
                            "date": day_str,
                            "user_initiated": 0,
                            "template_initiated": 0,
                            "template_converted": 0,
                            "total_billable": 0
                        }

                    daily_data[day_str][conv_type] = count

                    if conv_type in ("user_initiated", "template_converted"):
                        daily_data[day_str]["total_billable"] += count

                return list(daily_data.values())

    except Exception as e:
        logger.error(f"[BILLING] Error getting daily breakdown: {e}")
        return []


async def aexport_billing_report_csv(
    client_id: str,
    start_date: datetime,
    end_date: datetime,
    output_file: str
) -> bool:
    """Export billing report to CSV file (async)."""
    try:
        import csv

        summary = await aget_billing_summary_for_date_range(client_id, start_date, end_date)

        if not summary:
            logger.error("[BILLING] No summary data to export")
            return False

        with open(output_file, 'w', newline='') as csvfile:
            writer = csv.writer(csvfile)

            writer.writerow(['Billing Report'])
            writer.writerow(['Client ID', summary['client_id']])
            writer.writerow(['Period', f"{start_date.date()} to {end_date.date()}"])
            writer.writerow(['Total Billable', summary['total_billable_conversations']])
            writer.writerow([])

            writer.writerow(['Conversation Type Breakdown'])
            writer.writerow(['Type', 'Count'])
            breakdown = summary.get('conversation_breakdown', {})
            for conv_type, count in breakdown.items():
                writer.writerow([conv_type, count])
            writer.writerow([])

            writer.writerow(['Template Statistics'])
            template_stats = summary.get('template_stats', {})
            for key, value in template_stats.items():
                writer.writerow([key, value])
            writer.writerow([])

            writer.writerow(['Daily Breakdown'])
            writer.writerow(['Date', 'User Initiated', 'Template Initiated', 'Template Converted', 'Total Billable'])
            daily_data = summary.get('daily_breakdown', [])
            for day in daily_data:
                writer.writerow([
                    day['date'],
                    day['user_initiated'],
                    day['template_initiated'],
                    day['template_converted'],
                    day['total_billable']
                ])

        logger.info(f"[BILLING] ✅ Report exported to {output_file}")
        return True

    except Exception as e:
        logger.error(f"[BILLING] ❌ Error exporting report: {e}", exc_info=True)
        return False

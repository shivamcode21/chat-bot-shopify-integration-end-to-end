"""Unit tests for the agent-only escalation follow-up context.

Covers:
  * Inertness — no records / feature disabled / no client_id ⇒ empty block,
    i.e. byte-for-byte prior behaviour for the overwhelming majority of turns.
  * Issue threading — rows sharing an order_id collapse into ONE entry even when
    their categories drift (Delivery Query -> Order Delivery Delayed ->
    Frustration), with the wait measured from the earliest member.
  * Sort + truncation — unresolved can never be evicted by newer resolved rows,
    and any remainder is disclosed rather than silently dropped.
  * Phone spelling variants — bare-10 and country-code forms of one number both
    match (production has 110 customers split across two spellings).
  * Tenant isolation — client_id is in the predicate and the cache key.
  * Follow-up lineage + alert cooldown — the ROW is always written; only the
    staff alert is rate-limited.
  * Graceful degradation — a DB failure yields an empty block, never an
    exception into the node.

See design_docs/ESCALATION_FOLLOW_UP_CONTEXT.md.
"""

from datetime import datetime, timedelta, timezone

import fashion_bot.utils.escalation_context as ec


NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)


# ── helpers ────────────────────────────────────────────────────────────────


def _rec(
    eid="e1",
    category="Delivery Query",
    status="unresolved",
    hours_ago=18,
    order_id=None,
    resolution_text=None,
    reason=None,
    resolved_hours_ago=None,
):
    return ec.EscalationRecord(
        escalation_id=eid,
        category=category,
        status=status,
        raised_at=NOW - timedelta(hours=hours_ago),
        resolved_at=(
            NOW - timedelta(hours=resolved_hours_ago)
            if resolved_hours_ago is not None
            else None
        ),
        resolution_text=resolution_text,
        reason=reason,
        order_id=order_id,
    )


def _patch_client_flag(monkeypatch, value=None):
    """Serve the per-client ``escalation_context_enabled`` override in-memory.

    ``ais_enabled_for_client`` imports ``aget_config`` lazily from config_manager,
    so patching the module attribute is enough (same approach as
    ``test_escalation_routing._patch_config``). Without this the flag read would
    reach for a real DB connection.
    """
    from fashion_bot import config_manager

    async def _fake_aget_config(key, client_id=None, **kwargs):
        return value if key == "escalation_context_enabled" else None

    monkeypatch.setattr(config_manager, "aget_config", _fake_aget_config)


def _stub_cache(monkeypatch, rows_json, client_flag=None):
    """Serve the snapshot from an in-memory payload, bypassing Redis and Postgres."""
    _patch_client_flag(monkeypatch, client_flag)
    monkeypatch.setattr(ec, "_redis_get", lambda key: _async(None))
    monkeypatch.setattr(ec, "_redis_set", lambda key, value, ttl: _async(None))
    monkeypatch.setattr(ec, "_load_rows_json", lambda cid, variants: _async(rows_json))


async def _async(value):
    return value


def _uniq(name):
    """Distinct client/phone per test so the process-local memory tier can't leak."""
    return f"11111111-0000-0000-0000-{abs(hash(name)) % 10**12:012d}"


# ── inertness ──────────────────────────────────────────────────────────────


def test_no_threads_renders_empty_block():
    assert ec.render_escalation_context_block([]) == ""


async def test_missing_client_id_returns_no_records(monkeypatch):
    called = []
    monkeypatch.setattr(
        ec, "_load_rows_json", lambda cid, v: called.append(cid) or _async("[]")
    )
    records = await ec.aget_escalation_records(client_id=None, phone_number="919876543210")
    assert records == []
    assert called == [], "must never issue an unscoped cross-tenant query"


async def test_disabled_flag_short_circuits(monkeypatch):
    monkeypatch.setattr(ec, "is_escalation_context_enabled", lambda: False)
    called = []
    _stub_cache(monkeypatch, "[]")
    monkeypatch.setattr(
        ec, "_load_rows_json", lambda cid, v: called.append(cid) or _async("[]")
    )
    records = await ec.aget_escalation_records(
        client_id=_uniq("disabled"), phone_number="919876543210"
    )
    assert records == []
    assert called == []


async def test_client_override_can_disable_one_tenant(monkeypatch):
    monkeypatch.setattr(ec, "is_escalation_context_enabled", lambda: True)
    called = []
    _stub_cache(monkeypatch, "[]", client_flag="false")
    monkeypatch.setattr(
        ec, "_load_rows_json", lambda cid, v: called.append(cid) or _async("[]")
    )
    assert await ec.aget_escalation_records(
        client_id=_uniq("optout"), phone_number="919876543210"
    ) == []
    assert called == []


async def test_env_off_performs_no_io_at_all(monkeypatch):
    """The kill switch must be a hard off, not merely an empty result."""
    monkeypatch.setattr(ec, "is_escalation_context_enabled", lambda: False)

    def _explode(*args, **kwargs):
        raise AssertionError("no I/O may happen when the feature is disabled")

    from fashion_bot import config_manager

    monkeypatch.setattr(config_manager, "aget_config", _explode)
    monkeypatch.setattr(ec, "_redis_get", _explode)
    monkeypatch.setattr(ec, "_load_rows_json", _explode)

    assert await ec.ais_enabled_for_client(_uniq("hardoff")) is False
    assert await ec.aget_escalation_records(
        client_id=_uniq("hardoff"), phone_number="919876543210"
    ) == []


async def test_no_phone_returns_no_records(monkeypatch):
    assert await ec.aget_escalation_records(client_id=_uniq("nophone"), phone_number="") == []


# ── phone spelling variants ────────────────────────────────────────────────


def test_phone_variants_cover_both_stored_spellings():
    variants = ec.phone_match_variants("919876543210")
    assert "9876543210" in variants and "919876543210" in variants
    assert "+919876543210" in variants
    # The bare-10 spelling of the same number produces a matching set.
    assert set(ec.phone_match_variants("9876543210")) <= set(variants) | {"9876543210"}


def test_web_session_identity_passes_through():
    assert ec.phone_match_variants("web_abc123") == ["web_abc123"]
    assert ec.escalation_identity("web_abc123") == "web_abc123"
    assert ec.escalation_identity("+91 98765 43210") == "9876543210"


def test_web_session_id_is_truncated_to_match_what_was_stored():
    """escalations.customer_phone is VARCHAR(20) and alog_escalation truncates.

    The widget generates 'web_' + uuid4() = 40 chars, so a lookup on the full
    session id can never match its own write. 87 of the 271 web rows in
    production are truncated UUIDs like 'web_02095d1c-2e07-4d'. fbw_ ids are
    naturally 20 chars, which is why this failed for only one web surface.
    """
    full = "web_02095d1c-2e07-4d01-8f9a-1234567890ab"
    assert len(full) == 40
    stored = full[:20]
    assert stored == "web_02095d1c-2e07-4d"
    assert ec.phone_match_variants(full) == [stored]
    assert ec.escalation_identity(full) == stored


def test_long_phone_variants_are_also_truncated():
    """A raw value over 20 chars must not be searched for verbatim either."""
    for v in ec.phone_match_variants("+91 98765 43210 ext 0001234"):
        assert len(v) <= 20


# ── issue threading ────────────────────────────────────────────────────────


def test_rows_on_one_order_collapse_to_one_thread():
    """62% of the multi-escalation case: one order, drifting category labels."""
    records = [
        _rec("e1", "Delivery Query", hours_ago=18, order_id="gv16384", reason="waiting 13 days"),
        _rec("e2", "Order Delivery Delayed", hours_ago=10, order_id="gv16384"),
        _rec("e3", "Frustration", hours_ago=2, order_id="gv16384"),
    ]
    threads = ec.build_threads(records)
    assert len(threads) == 1
    t = threads[0]
    assert t.thread_key == "order:gv16384"
    assert t.root_escalation_id == "e1", "root is the earliest member"
    assert t.category == "Frustration", "category tracks the latest member"
    assert t.chase_count == 2
    assert round(t.hours_waiting(NOW)) == 18, "wait measured from the earliest member"


def test_orderless_rows_thread_by_category():
    records = [
        _rec("e1", "Restocking Query", hours_ago=5),
        _rec("e2", "Restocking Query", hours_ago=1),
        _rec("e3", "Bulk Order Discount", hours_ago=3),
    ]
    threads = ec.build_threads(records)
    assert len(threads) == 2
    assert {t.thread_key for t in threads} == {"cat:restocking_query", "cat:bulk_order_discount"}


def test_thread_is_unresolved_if_any_member_is():
    records = [
        _rec("e1", status="resolved", hours_ago=20, order_id="gv1", resolved_hours_ago=15),
        _rec("e2", status="unresolved", hours_ago=4, order_id="gv1"),
    ]
    (thread,) = ec.build_threads(records)
    assert thread.is_unresolved
    assert thread.resolution_text is None, "an open thread must not present a stale resolution"


def test_resolved_thread_keeps_latest_resolution_text():
    records = [
        _rec("e1", status="resolved", hours_ago=30, order_id="gv1",
             resolved_hours_ago=28, resolution_text="checking"),
        _rec("e2", status="resolved", hours_ago=20, order_id="gv1",
             resolved_hours_ago=10, resolution_text="reverted and dispatch today"),
    ]
    (thread,) = ec.build_threads(records)
    assert not thread.is_unresolved
    assert thread.resolution_text == "reverted and dispatch today"


# ── sort + truncation ──────────────────────────────────────────────────────


def test_unresolved_never_truncated_by_newer_resolved():
    """Sorting by date alone would evict the one record the agent needs."""
    records = [_rec("old", "Delivery Query", hours_ago=240, order_id="gv-old")]
    records += [
        _rec(f"r{i}", "Return Request", status="resolved", hours_ago=i + 1,
             order_id=f"gv-r{i}", resolved_hours_ago=i, resolution_text="done")
        for i in range(6)
    ]
    block = ec.render_escalation_context_block(ec.build_threads(records), now=NOW)
    assert "gv-old" in block
    assert "UNRESOLVED" in block
    assert "not shown" in block, "remainder must be disclosed, never silently dropped"


def test_longest_waiting_unresolved_is_first():
    records = [
        _rec("recent", "Return Request", hours_ago=2, order_id="gv-new"),
        _rec("stale", "Delivery Query", hours_ago=100, order_id="gv-stale"),
    ]
    threads = ec.build_threads(records)
    assert threads[0].order_id == "gv-stale"


# ── rendering ──────────────────────────────────────────────────────────────


def test_block_renders_status_order_and_chases():
    records = [
        _rec("e1", "Delivery Query", hours_ago=18, order_id="gv16384",
             reason="Customer waiting 13 days, no tracking movement"),
        _rec("e2", "Delivery Query", hours_ago=3, order_id="gv16384"),
    ]
    block = ec.render_escalation_context_block(ec.build_threads(records), now=NOW)
    assert "NEVER MENTION" in block
    assert "STATUS: UNRESOLVED" in block
    assert "order gv16384" in block
    assert "18 hours ago" in block
    assert "chased this 1 time." in block
    assert "escalate_to_agent" in block, "the block must tell the agent how to act"


def test_unresolved_block_defers_to_a_teammate_reply_in_the_transcript():
    """`status` and the transcript can disagree.

    A human agent replying via /dashboard/api/send-reply appends their message to
    state["messages"] (as an AIMessage) but does NOT flip escalations.status —
    marking resolved is a separate click that ops routinely skips. The block must
    therefore not assert "no human update" as fact over the visible conversation.
    """
    records = [_rec("e1", "Delivery Query", hours_ago=6, order_id="gv1")]
    block = ec.render_escalation_context_block(ec.build_threads(records), now=NOW)
    assert "CHECK FOR A TEAMMATE REPLY FIRST" in block
    assert "not marked it" in block
    assert "do NOT re-escalate unless the customer says it" in block


def test_human_reply_is_surfaced_in_the_block():
    """The whole point: after 24h the chat history is gone from Redis, so the
    teammate's answer has to come from Postgres or the agent cannot see it."""
    records = [_rec("e1", "Delivery Query", hours_ago=48, order_id="gv1")]
    replies = [
        ec.HumanReply(
            sent_at=NOW - timedelta(hours=20),
            text="we have reverted the shipment, will dispatch today",
        )
    ]
    block = ec.render_escalation_context_block(
        ec.build_threads(records), human_replies=replies, now=NOW
    )
    # The ⚠️ prefix distinguishes the rendered section from step 3a's reference
    # to it inside the instructions.
    assert "⚠️ A HUMAN TEAMMATE HAS ALREADY REPLIED" in block
    assert "reverted the shipment" in block
    assert "Treat this as the update" in block
    # The status line still tells the truth about the ticket.
    assert "STATUS: UNRESOLVED" in block


def test_no_human_reply_section_when_there_are_none():
    records = [_rec("e1", "Delivery Query", hours_ago=48, order_id="gv1")]
    block = ec.render_escalation_context_block(
        ec.build_threads(records), human_replies=[], now=NOW
    )
    assert "⚠️ A HUMAN TEAMMATE HAS ALREADY REPLIED" not in block
    assert "Treat this as the update" not in block


def test_human_reply_text_is_flattened_and_capped():
    records = [_rec("e1", "Delivery Query", hours_ago=48, order_id="gv1")]
    replies = [ec.HumanReply(sent_at=NOW, text="line one\nline two " + "x" * 400)]
    block = ec.render_escalation_context_block(
        ec.build_threads(records), human_replies=replies, now=NOW
    )
    line = [ln for ln in block.splitlines() if ln.strip().startswith("›")][0]
    assert "\n" not in line and len(line) < 320


def test_resolved_without_text_says_not_recorded():
    """230 of 838 resolved rows in production carry no resolution_text."""
    records = [_rec("e1", status="resolved", hours_ago=20, resolved_hours_ago=2)]
    block = ec.render_escalation_context_block(ec.build_threads(records), now=NOW)
    assert "STATUS: RESOLVED" in block
    assert "not recorded — do not guess" in block


def test_resolved_with_text_is_quoted():
    records = [
        _rec("e1", status="resolved", hours_ago=20, resolved_hours_ago=2,
             resolution_text="reverted and dispatch today")
    ]
    block = ec.render_escalation_context_block(ec.build_threads(records), now=NOW)
    assert '"reverted and dispatch today"' in block


def test_block_caps_at_max_threads(monkeypatch):
    monkeypatch.setattr(ec, "_MAX_THREADS", 2)
    records = [
        _rec(f"e{i}", "Delivery Query", hours_ago=i + 1, order_id=f"gv{i}") for i in range(5)
    ]
    block = ec.render_escalation_context_block(ec.build_threads(records), now=NOW)
    assert block.count("STATUS:") == 2
    # "older issue(s)", not "older resolved issue(s)": every record here is
    # UNRESOLVED, so the old wording told the agent something false about what it
    # was not being shown.
    assert "(+3 older issue(s) not shown)" in block


def test_single_line_summary_is_flattened_and_capped():
    records = [_rec("e1", reason="line one\nline two   with   spaces " + "x" * 400)]
    block = ec.render_escalation_context_block(ec.build_threads(records), now=NOW)
    handed = [ln for ln in block.splitlines() if "Handed over as" in ln]
    assert len(handed) == 1
    assert "\n" not in handed[0] and len(handed[0]) < 300


# ── thread matching (drives re-escalation lineage) ─────────────────────────


def test_matching_prefers_order_over_category():
    threads = ec.build_threads([
        _rec("e1", "Delivery Query", hours_ago=18, order_id="gv16384"),
        _rec("e2", "Restocking Query", hours_ago=5),
    ])
    match = ec.find_matching_thread(threads, order_id="gv16384", category="Frustration")
    assert match is not None and match.thread_key == "order:gv16384"


def test_matching_returns_none_for_a_different_order():
    threads = ec.build_threads([_rec("e1", "Delivery Query", hours_ago=18, order_id="gv16384")])
    assert ec.find_matching_thread(threads, order_id="gv99999", category="Delivery Query") is None


def test_matching_ignores_resolved_threads():
    threads = ec.build_threads([
        _rec("e1", "Delivery Query", status="resolved", hours_ago=20,
             order_id="gv1", resolved_hours_ago=2)
    ])
    assert ec.find_matching_thread(threads, order_id="gv1", category="Delivery Query") is None


# ── cached read ────────────────────────────────────────────────────────────


async def test_records_parsed_from_cached_payload(monkeypatch):
    import json

    cid = _uniq("parsed")
    _stub_cache(monkeypatch, json.dumps([
        {
            "escalation_id": "e1",
            "category": "Delivery Query",
            "status": "unresolved",
            "reason": "waiting",
            "raised_at": (NOW - timedelta(hours=4)).isoformat(),
            "resolved_at": None,
            "resolution_text": None,
            "order_id": "gv16384",
        }
    ]))
    records = await ec.aget_escalation_records(client_id=cid, phone_number="919876543210")
    assert len(records) == 1
    assert records[0].order_id == "gv16384"
    assert records[0].is_unresolved


async def test_db_failure_degrades_to_empty(monkeypatch):
    def _boom(cid, variants):
        raise RuntimeError("db down")

    _stub_cache(monkeypatch, "[]")
    monkeypatch.setattr(ec, "_load_rows_json", _boom)
    records = await ec.aget_escalation_records(
        client_id=_uniq("boom"), phone_number="919876543210"
    )
    assert records == []


async def test_block_helper_returns_empty_when_no_records(monkeypatch):
    _stub_cache(monkeypatch, "[]")
    block = await ec.aget_escalation_context_block(
        client_id=_uniq("emptyblock"), phone_number="919876543210"
    )
    assert block == ""


async def test_empty_result_is_recached_with_the_long_ttl(monkeypatch):
    """~95% of turns answer 'no escalations'; keep them off the DB.

    An empty answer can only stop being true when an escalation is inserted, and
    every insert path busts the key — so it is safe to hold far longer than a
    positive answer, whose status a human can flip at any moment.
    """
    sets = []
    _patch_client_flag(monkeypatch, None)
    monkeypatch.setattr(ec, "_redis_get", lambda key: _async(None))
    monkeypatch.setattr(
        ec, "_redis_set", lambda key, value, ttl: sets.append((value, ttl)) or _async(None)
    )
    monkeypatch.setattr(ec, "_load_rows_json", lambda cid, v: _async("[]"))

    assert await ec.aget_escalation_records(
        client_id=_uniq("negttl"), phone_number="919876543210"
    ) == []
    assert sets, "the empty answer must be written to redis"
    assert sets[-1] == ("[]", ec._EMPTY_TTL_SECONDS)
    assert ec._EMPTY_TTL_SECONDS > ec._TTL_SECONDS


async def test_non_empty_result_keeps_the_short_ttl(monkeypatch):
    """A live escalation can be resolved by a human at any time — stay fresh."""
    import json

    sets = []
    payload = json.dumps([
        {
            "escalation_id": "e1", "category": "Delivery Query", "status": "unresolved",
            "reason": None, "raised_at": (NOW - timedelta(hours=1)).isoformat(),
            "resolved_at": None, "resolution_text": None, "order_id": "gv1",
        }
    ])
    _patch_client_flag(monkeypatch, None)
    monkeypatch.setattr(ec, "_redis_get", lambda key: _async(None))
    monkeypatch.setattr(
        ec, "_redis_set", lambda key, value, ttl: sets.append((value, ttl)) or _async(None)
    )
    monkeypatch.setattr(ec, "_load_rows_json", lambda cid, v: _async(payload))

    records = await ec.aget_escalation_records(
        client_id=_uniq("posttl"), phone_number="919876543210"
    )
    assert len(records) == 1
    assert all(ttl == ec._TTL_SECONDS for _, ttl in sets), (
        "a positive result must not inherit the long negative TTL"
    )


async def test_bundle_skips_the_human_reply_query_when_nothing_is_open(monkeypatch):
    """The ~95% case must not pay for a lookup that cannot change the answer."""
    import json

    called = []
    _stub_cache(monkeypatch, json.dumps([
        {
            "escalation_id": "e1", "category": "Delivery Query", "status": "resolved",
            "reason": None, "raised_at": (NOW - timedelta(hours=40)).isoformat(),
            "resolved_at": (NOW - timedelta(hours=2)).isoformat(),
            "resolution_text": "done", "order_id": "gv1",
            "conversation_id": "11111111-1111-1111-1111-111111111111",
        }
    ]))
    monkeypatch.setattr(
        ec, "_load_human_replies_json",
        lambda cid, convs, since: called.append(cid) or _async("[]"),
    )
    bundle = await ec.aprefetch_escalation_context(
        client_id=_uniq("noopen"), phone_number="919876543210"
    )
    assert bundle.records and not bundle.has_unresolved
    assert bundle.human_replies == []
    assert called == [], "no unresolved issue ⇒ no human-reply query"


async def test_bundle_fetches_human_replies_when_something_is_open(monkeypatch):
    import json

    conv = "11111111-1111-1111-1111-111111111111"
    _stub_cache(monkeypatch, json.dumps([
        {
            "escalation_id": "e1", "category": "Delivery Query", "status": "unresolved",
            "reason": None, "raised_at": (NOW - timedelta(hours=48)).isoformat(),
            "resolved_at": None, "resolution_text": None, "order_id": "gv1",
            "conversation_id": conv,
        }
    ]))
    seen = {}

    def _load(cid, convs, since):
        seen["convs"] = convs
        seen["since"] = since
        return _async(json.dumps([
            {"sent_at": (NOW - timedelta(hours=20)).isoformat(), "text": "reverted, dispatching"}
        ]))

    monkeypatch.setattr(ec, "_load_human_replies_json", _load)
    bundle = await ec.aprefetch_escalation_context(
        client_id=_uniq("openhr"), phone_number="919876543210"
    )
    assert bundle.has_unresolved
    assert [r.text for r in bundle.human_replies] == ["reverted, dispatching"]
    assert seen["convs"] == [conv], "scoped to the escalation's own conversation (indexed)"
    assert seen["since"] is not None, "only messages after the issue was raised"


async def test_payload_cached_before_conversation_id_existed_still_parses(monkeypatch):
    """Old cache entries must not break on the new field."""
    import json

    _stub_cache(monkeypatch, json.dumps([
        {
            "escalation_id": "e1", "category": "Delivery Query", "status": "unresolved",
            "reason": None, "raised_at": NOW.isoformat(), "resolved_at": None,
            "resolution_text": None, "order_id": "gv1",
        }
    ]))
    records = await ec.aget_escalation_records(
        client_id=_uniq("oldpayload"), phone_number="919876543210"
    )
    assert len(records) == 1 and records[0].conversation_id is None


async def test_human_reply_failure_degrades_to_empty(monkeypatch):
    import json

    _stub_cache(monkeypatch, json.dumps([
        {
            "escalation_id": "e1", "category": "Delivery Query", "status": "unresolved",
            "reason": None, "raised_at": (NOW - timedelta(hours=48)).isoformat(),
            "resolved_at": None, "resolution_text": None, "order_id": "gv1",
            "conversation_id": "11111111-1111-1111-1111-111111111111",
        }
    ]))

    def _boom(cid, convs, since):
        raise RuntimeError("messages table down")

    monkeypatch.setattr(ec, "_load_human_replies_json", _boom)
    bundle = await ec.aprefetch_escalation_context(
        client_id=_uniq("hrboom"), phone_number="919876543210"
    )
    assert bundle.has_unresolved, "the escalation itself must still surface"
    assert bundle.human_replies == []


async def test_memory_tier_is_short_lived_so_a_cross_pod_bust_wins(monkeypatch):
    """The memory tier is process-local: a bust on pod A cannot reach pod B.

    AGENTS.md §3 accepts that for client configs ("bust Redis, then local cache
    expires via TTL"), but here a stale local "[]" would hide an escalation the
    bot just raised and no-op the feature for any turn landing on another pod.
    So the long TTLs live in Redis, where the bust IS global, and memory is kept
    to seconds. Worst-case cross-pod staleness == the memory TTL.
    """
    seen = {}

    async def _fake_tiered(**kwargs):
        seen.update(kwargs)
        await kwargs["set_to_redis_fn"]("[]")
        return "[]", "source"

    _patch_client_flag(monkeypatch, None)
    monkeypatch.setattr(ec, "aget_with_tiered_cache", _fake_tiered)
    redis_writes = []
    monkeypatch.setattr(
        ec, "_redis_set",
        lambda key, value, ttl: redis_writes.append(ttl) or _async(None),
    )

    await ec.aget_escalation_records(
        client_id=_uniq("multipod"), phone_number="919876543210"
    )

    assert seen["ttl_seconds"] == ec._MEMORY_TTL_SECONDS, (
        "the helper's ttl_seconds governs the MEMORY tier only"
    )
    assert redis_writes == [ec._EMPTY_TTL_SECONDS], "the long TTL must land in Redis"
    assert ec._MEMORY_TTL_SECONDS < ec._TTL_SECONDS < ec._EMPTY_TTL_SECONDS
    assert ec._MEMORY_TTL_SECONDS <= 30, "memory must not outlive a turn by much"


def test_redis_ttl_is_chosen_by_value():
    assert ec._redis_ttl_for("[]") == ec._EMPTY_TTL_SECONDS
    assert ec._redis_ttl_for("") == ec._EMPTY_TTL_SECONDS
    assert ec._redis_ttl_for('[{"escalation_id":"e1"}]') == ec._TTL_SECONDS


def test_human_reply_cache_key_includes_the_query_scope():
    """Without the scope in the key, a NEW escalation would reuse replies fetched
    under the PREVIOUS issue's conversation/since for the rest of the TTL."""
    a = ec._scope_hash(["conv-a"], NOW)
    assert a == ec._scope_hash(["conv-a"], NOW), "stable for the same scope"
    assert a != ec._scope_hash(["conv-b"], NOW), "different conversation ⇒ different key"
    assert a != ec._scope_hash(["conv-a"], NOW - timedelta(hours=1)), "different since"
    assert ec._scope_hash(["a", "b"], NOW) == ec._scope_hash(["b", "a"], NOW), "order-insensitive"


async def test_human_replies_rescoped_after_a_new_escalation(monkeypatch):
    import json

    keys = []

    async def _fake_tiered(**kwargs):
        keys.append(kwargs["cache_key"])
        return json.dumps([]), "source"

    _patch_client_flag(monkeypatch, None)
    monkeypatch.setattr(ec, "aget_with_tiered_cache", _fake_tiered)
    common = dict(client_id="c1", phone_number="919876543210")

    await ec.aget_human_replies(conversation_ids=["conv-old"], since=NOW, **common)
    await ec.aget_human_replies(
        conversation_ids=["conv-new"], since=NOW + timedelta(minutes=5), **common
    )
    assert keys[0] != keys[1], "a new issue must not reuse the old issue's replies"
    assert all(k.startswith(ec.HUMAN_REPLY_CACHE_PREFIX) for k in keys)


async def test_status_lookup_reports_nothing_when_window_is_empty(monkeypatch):
    _stub_cache(monkeypatch, "[]")
    out = await ec.aget_escalation_status(
        client_id=_uniq("statusnone"), phone_number="919876543210"
    )
    assert out["found"] is False and out["count"] == 0 and out["issues"] == []
    assert "do NOT invent" in out["guidance"].lower() or "Do NOT invent" in out["guidance"]


async def test_status_lookup_projects_the_same_threads(monkeypatch):
    """The tool is a projection of the block's data, not a second source of truth."""
    import json

    conv = "11111111-1111-1111-1111-111111111111"
    _stub_cache(monkeypatch, json.dumps([
        {
            "escalation_id": "root", "category": "Delivery Query", "status": "unresolved",
            "reason": "waiting 13 days",
            "raised_at": (NOW - timedelta(hours=48)).isoformat(),
            "resolved_at": None, "resolution_text": None,
            "order_id": "gv16384", "conversation_id": conv,
        },
        {
            "escalation_id": "chase", "category": "Frustration", "status": "unresolved",
            "reason": "still nothing",
            "raised_at": (NOW - timedelta(hours=2)).isoformat(),
            "resolved_at": None, "resolution_text": None,
            "order_id": "gv16384", "conversation_id": conv,
        },
    ]))
    monkeypatch.setattr(ec, "_load_human_replies_json", lambda c, v, s: _async("[]"))

    out = await ec.aget_escalation_status(
        client_id=_uniq("statusone"), phone_number="919876543210", now=NOW
    )
    assert out["found"] is True
    assert out["count"] == 1, "two rows on one order are ONE issue, same as the block"
    issue = out["issues"][0]
    assert issue["order_id"] == "gv16384"
    assert issue["status"] == "unresolved"
    assert issue["times_chased"] == 1
    assert issue["waiting_hours"] == 48.0, "measured from the earliest row"
    assert issue["resolution_recorded"] is False


async def test_email_only_web_escalation_keeps_a_reachable_contact(monkeypatch):
    """Web chat has no reachable identifier of its own.

    The row's customer_phone is the truncated session id. A phone links the
    session and replaces it; an EMAIL cannot be an identity key, so without
    persisting it the only contact is buried in whatsapp_message free text and
    ops has nothing to reach the customer by.
    """
    import json

    _stub_cache(monkeypatch, json.dumps([
        {
            "escalation_id": "e1", "category": "Delivery Query", "status": "unresolved",
            "reason": "customer wants a manager",
            "raised_at": (NOW - timedelta(hours=3)).isoformat(),
            "resolved_at": None, "resolution_text": None, "order_id": None,
            "conversation_id": None, "customer_contact": "john.doe+cx@example.co.in",
        }
    ]))
    out = await ec.aget_escalation_status(
        client_id=_uniq("emailonly"), phone_number="web_02095d1c-2e07-4d", now=NOW
    )
    assert out["issues"][0]["contact_on_file"] == "john.doe+cx@example.co.in"


def test_thread_takes_the_latest_contact():
    """A customer correcting their address mid-thread must not keep the stale one."""
    old = ec.EscalationRecord(
        escalation_id="e1", category="Delivery Query", status="unresolved",
        raised_at=NOW - timedelta(hours=5), resolved_at=None, resolution_text=None,
        reason="x", order_id="gv1", customer_contact="typo@exmaple.com",
    )
    new = ec.EscalationRecord(
        escalation_id="e2", category="Delivery Query", status="unresolved",
        raised_at=NOW - timedelta(hours=1), resolved_at=None, resolution_text=None,
        reason="x", order_id="gv1", customer_contact="correct@example.com",
    )
    (thread,) = ec.build_threads([old, new])
    assert thread.customer_contact == "correct@example.com"


def test_payload_without_customer_contact_still_parses():
    """Rows and cached payloads predating the field must not break."""
    import json

    recs = ec._records_from_json(json.dumps([
        {
            "escalation_id": "e1", "category": "Delivery Query", "status": "unresolved",
            "reason": None, "raised_at": NOW.isoformat(), "resolved_at": None,
            "resolution_text": None, "order_id": None,
        }
    ]))
    assert len(recs) == 1 and recs[0].customer_contact is None


async def test_status_lookup_never_exposes_internal_ids(monkeypatch):
    """The agent must not be able to read a ticket id out to a customer."""
    import json

    _stub_cache(monkeypatch, json.dumps([
        {
            "escalation_id": "ffc5bc67-de7c-4f88-baab-b4fc314dfd5b",
            "category": "Delivery Query", "status": "unresolved", "reason": "x",
            "raised_at": (NOW - timedelta(hours=5)).isoformat(),
            "resolved_at": None, "resolution_text": None,
            "order_id": "gv1", "conversation_id": None,
        }
    ]))
    out = await ec.aget_escalation_status(
        client_id=_uniq("statusids"), phone_number="919876543210", now=NOW
    )
    assert "ffc5bc67" not in json.dumps(out)
    assert "escalation_id" not in json.dumps(out)


async def test_status_lookup_surfaces_a_teammate_reply(monkeypatch):
    import json

    conv = "11111111-1111-1111-1111-111111111111"
    _stub_cache(monkeypatch, json.dumps([
        {
            "escalation_id": "e1", "category": "Delivery Query", "status": "unresolved",
            "reason": "x", "raised_at": (NOW - timedelta(hours=30)).isoformat(),
            "resolved_at": None, "resolution_text": None,
            "order_id": "gv1", "conversation_id": conv,
        }
    ]))
    monkeypatch.setattr(
        ec, "_load_human_replies_json",
        lambda c, v, s: _async(json.dumps(
            [{"sent_at": (NOW - timedelta(hours=4)).isoformat(), "text": "reverted, dispatching"}]
        )),
    )
    out = await ec.aget_escalation_status(
        client_id=_uniq("statushr"), phone_number="919876543210", now=NOW
    )
    assert out["human_replies_since_raised"][0]["text"] == "reverted, dispatching"
    assert "restate it" in out["guidance"]


async def test_status_lookup_respects_the_internal_category_filter(monkeypatch):
    """An FYI must not become an answerable 'status' for the customer."""
    import json

    _stub_cache(monkeypatch, json.dumps([
        {
            "escalation_id": "e1", "category": "Offline Store Suggestion",
            "status": "unresolved", "reason": "store visit suggested",
            "raised_at": (NOW - timedelta(hours=3)).isoformat(),
            "resolved_at": None, "resolution_text": None,
            "order_id": None, "conversation_id": None,
        }
    ]))
    out = await ec.aget_escalation_status(
        client_id=_uniq("statusfyi"), phone_number="919876543210"
    )
    assert out["found"] is False


async def test_prefetch_never_raises(monkeypatch):
    """It runs as a detached task; an escaping exception would be a noisy warning."""
    def _boom(**kwargs):
        raise RuntimeError("anything at all")

    monkeypatch.setattr(ec, "aget_escalation_records", _boom)
    assert await ec.aprefetch_escalation_records(
        client_id="c1", phone_number="919876543210"
    ) == []


async def test_internal_only_categories_never_reach_the_block(monkeypatch):
    """Offline Store Suggestion is an FYI, not a hand-off the customer is awaiting.

    45 rows in production, 40 unresolved (nobody resolves an FYI). Surfacing one
    would have the agent tell a customer "our team has your issue, no update yet"
    about something nobody ever promised them — and re-escalate it as URGENT.
    Note these are not in the tool enum, so normalize_escalation_category maps them
    to "General": unfiltered they rendered as an anonymous open issue.
    """
    import json

    _stub_cache(monkeypatch, json.dumps([
        {
            "escalation_id": "e1", "category": "Offline Store Suggestion",
            "status": "unresolved", "reason": "Store visit suggested",
            "raised_at": (NOW - timedelta(hours=3)).isoformat(), "resolved_at": None,
            "resolution_text": None, "order_id": None, "conversation_id": None,
        }
    ]))
    records = await ec.aget_escalation_records(
        client_id=_uniq("fyi"), phone_number="919876543210"
    )
    assert records == []


async def test_lead_gen_handoffs_are_still_surfaced(monkeypatch):
    """Bulk / B2B DO tell the customer "our team will contact you soon",
    so a follow-up on one must be handled — they are not internal FYIs."""
    import json

    _stub_cache(monkeypatch, json.dumps([
        {
            "escalation_id": "e1", "category": "Bulk Order Discount",
            "status": "unresolved", "reason": "wants bulk pricing",
            "raised_at": (NOW - timedelta(hours=3)).isoformat(), "resolved_at": None,
            "resolution_text": None, "order_id": None, "conversation_id": None,
        }
    ]))
    records = await ec.aget_escalation_records(
        client_id=_uniq("bulk"), phone_number="919876543210"
    )
    assert len(records) == 1 and records[0].category == "Bulk Order Discount"


def test_internal_only_list_is_explicit():
    assert ec.INTERNAL_ONLY_CATEGORIES == frozenset(
        {"offline store suggestion", "walk-in appointment", "courier update pending"}
    )
    # Guard against someone quietly excluding a real customer hand-off.
    for keep in ("bulk order discount", "b2b order", "wholesale inquiry",
                 "order cancellation - non-integrated partner"):
        assert keep not in ec.INTERNAL_ONLY_CATEGORIES


async def test_guest_to_phone_migration_moves_escalation_rows(monkeypatch):
    """A guest escalation must follow the customer when they link a phone.

    The escalation context looks up the *current* identity. Without this the row
    stays under the guest session id and the bot greets a customer chasing a
    hand-off as if nothing had ever been escalated. Matches on both the full
    session id and its VARCHAR(20) prefix, since alog_escalation truncates.
    """
    from fashion_bot.history import postgres_conversations as pc

    executed = []

    class _Cur:
        rowcount = 2
        async def execute(self, sql, params=None):
            executed.append((" ".join(sql.split()), params))
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class _Conn:
        def cursor(self): return _Cur()
        async def commit(self): pass

    class _CM:
        async def __aenter__(self): return _Conn()
        async def __aexit__(self, *a): return False

    monkeypatch.setattr(pc, "get_async_postgres_connection", lambda: _CM())
    monkeypatch.setattr(pc, "_aensure_tables_exist", lambda conn: _async(None))

    session_id = "web_02095d1c-2e07-4d01-8f9a-1234567890ab"
    stats = await pc.amigrate_webchat_guest_to_phone(
        client_id="11111111-1111-1111-1111-111111111111",
        session_id=session_id,
        new_phone="9876543210",
    )

    esc = [(s, p) for s, p in executed if "UPDATE escalations" in s]
    assert len(esc) == 1, "escalations must be migrated alongside conversations/messages"
    sql, params = esc[0]
    assert "client_id = %s::uuid" in sql, "tenant-scoped"
    assert params[0] == "9876543210"
    # both the full id and the 20-char stored prefix are matched
    assert session_id in params and session_id[:20] in params
    assert stats["escalations_updated"] == 2


async def test_migration_survives_an_escalation_update_failure(monkeypatch):
    """Identity migration must not fail because the escalation backfill did."""
    from fashion_bot.history import postgres_conversations as pc

    class _Cur:
        rowcount = 1
        async def execute(self, sql, params=None):
            if "UPDATE escalations" in sql:
                raise RuntimeError("escalations locked")
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class _Conn:
        def cursor(self): return _Cur()
        async def commit(self): pass

    class _CM:
        async def __aenter__(self): return _Conn()
        async def __aexit__(self, *a): return False

    monkeypatch.setattr(pc, "get_async_postgres_connection", lambda: _CM())
    monkeypatch.setattr(pc, "_aensure_tables_exist", lambda conn: _async(None))

    stats = await pc.amigrate_webchat_guest_to_phone(
        client_id="11111111-1111-1111-1111-111111111111",
        session_id="web_abcdef",
        new_phone="9876543210",
    )
    assert stats["escalations_updated"] == 0
    assert stats["conversations_updated"] >= 1, "the primary migration still happened"


def test_cache_key_is_tenant_scoped():
    a = ec._cache_key("client-a", "9876543210")
    b = ec._cache_key("client-b", "9876543210")
    assert a != b and a.startswith(ec.CACHE_PREFIX)


# ── follow-up lineage + alert cooldown ─────────────────────────────────────


async def test_first_time_escalation_has_no_lineage(monkeypatch):
    _stub_cache(monkeypatch, "[]")
    lineage = await ec.aget_follow_up_lineage(
        client_id=_uniq("firsttime"),
        phone_number="919876543210",
        category="Delivery Query",
        order_id="gv16384",
    )
    assert lineage is None, "a first escalation must behave exactly as it does today"


async def test_repeat_chase_produces_lineage(monkeypatch):
    import json

    _stub_cache(monkeypatch, json.dumps([
        {
            "escalation_id": "root",
            "category": "Delivery Query",
            "status": "unresolved",
            "reason": "waiting",
            "raised_at": (NOW - timedelta(hours=18)).isoformat(),
            "resolved_at": None,
            "resolution_text": None,
            "order_id": "gv16384",
        }
    ]))
    lineage = await ec.aget_follow_up_lineage(
        client_id=_uniq("repeat"),
        phone_number="919876543210",
        category="Frustration",  # category drifted; order still matches
        order_id="gv16384",
        now=NOW,
    )
    assert lineage is not None
    assert lineage["follow_up_of"] == "root"
    assert lineage["follow_up_count"] == 1
    assert lineage["thread_key"] == "order:gv16384"
    assert lineage["hours_waiting"] == 18.0


async def test_first_chase_always_alerts_immediately(monkeypatch):
    """Alerting must be immediate for anything carrying new information.

    The original escalation never reaches this guard (no open thread to match),
    and the first chase is the call that SETS the key — so both alert. Only the
    third and later messages on one issue inside the window go quiet.
    """
    calls = []

    async def _fake_already_processed(key, *, namespace="webhook", ttl_seconds=None):
        calls.append(key)
        return len(calls) > 1  # first call: not seen

    import fashion_bot.workers.idempotency as idem

    monkeypatch.setattr(idem, "already_processed", _fake_already_processed)
    assert await ec.ashould_send_follow_up_alert(
        client_id="c1", phone_number="919876543210", thread_key="order:gv1"
    ) is True


async def test_cooldown_can_be_disabled(monkeypatch):
    """Operators who want every chase to ping can turn suppression off."""
    monkeypatch.setattr(ec, "_FOLLOWUP_COOLDOWN_MINUTES", 0)

    async def _explode(*args, **kwargs):
        raise AssertionError("guard must not be consulted when disabled")

    import fashion_bot.workers.idempotency as idem

    monkeypatch.setattr(idem, "already_processed", _explode)
    for _ in range(3):
        assert await ec.ashould_send_follow_up_alert(
            client_id="c1", phone_number="919876543210", thread_key="order:gv1"
        ) is True


async def test_cooldown_window_is_short_enough_to_only_catch_bursts(monkeypatch):
    """75% of real repeats land within 5 min, 83% within 15; the tail deserves a ping."""
    assert 0 < ec._FOLLOWUP_COOLDOWN_MINUTES <= 30


async def test_alert_cooldown_suppresses_second_chase(monkeypatch):
    seen = {}

    async def _fake_already_processed(key, *, namespace="webhook", ttl_seconds=None):
        full = f"{namespace}:{key}"
        first = full not in seen
        seen[full] = True
        return not first

    import fashion_bot.workers.idempotency as idem

    monkeypatch.setattr(idem, "already_processed", _fake_already_processed)

    kwargs = dict(
        client_id="c1", phone_number="919876543210", thread_key="order:gv16384"
    )
    assert await ec.ashould_send_follow_up_alert(**kwargs) is True
    assert await ec.ashould_send_follow_up_alert(**kwargs) is False
    # A different issue for the same customer still alerts.
    assert await ec.ashould_send_follow_up_alert(
        client_id="c1", phone_number="919876543210", thread_key="order:gv99999"
    ) is True


async def test_alert_guard_fails_open(monkeypatch):
    async def _boom(key, *, namespace="webhook", ttl_seconds=None):
        raise RuntimeError("redis down")

    import fashion_bot.workers.idempotency as idem

    monkeypatch.setattr(idem, "already_processed", _boom)
    try:
        allowed = await ec.ashould_send_follow_up_alert(
            client_id="c1", phone_number="919876543210", thread_key="order:gv1"
        )
    except RuntimeError:
        allowed = None
    assert allowed is not False, "a Redis failure must never silence a staff alert"


# ── shared idempotency guard stays backward compatible ─────────────────────


async def test_already_processed_ttl_override_is_optional(monkeypatch):
    import fashion_bot.workers.idempotency as idem

    captured = {}

    class _FakeRedis:
        async def set(self, key, value, nx=None, ex=None):
            captured["key"] = key
            captured["ex"] = ex
            return True

    monkeypatch.setattr(idem, "get_shared_async_redis_client", lambda: _async(_FakeRedis()))

    await idem.already_processed("abc", namespace="webhook")
    assert captured["ex"] == idem.WEBHOOK_DEDUP_TTL_SECONDS, "default unchanged"

    await idem.already_processed("abc", namespace="escalation_followup", ttl_seconds=10800)
    assert captured["ex"] == 10800


# ── contact already on file: stop re-asking a returning customer ───────────


def test_email_candidate_is_extracted_and_lowercased():
    from fashion_bot.utils.utils import extract_email_candidate

    assert extract_email_candidate("sure, it's Asha.K@Gmail.com") == "asha.k@gmail.com"
    assert extract_email_candidate("my number is 9876543210") is None
    assert extract_email_candidate("", None, "no contact here") is None


async def test_contact_on_file_returns_the_most_recent_one(monkeypatch):
    """A second escalation with a corrected address must win over the first."""
    import json as _json

    cid, phone = _uniq("contact-latest"), "web_02095d1c-2e07-4d01"
    rows = _json.dumps(
        [
            {
                "escalation_id": "e-old",
                "category": "Delivery Query",
                "status": "unresolved",
                "raised_at": (NOW - timedelta(days=3)).isoformat(),
                "customer_contact": "old@example.com",
            },
            {
                "escalation_id": "e-new",
                "category": "Delivery Query",
                "status": "unresolved",
                "raised_at": (NOW - timedelta(hours=2)).isoformat(),
                "customer_contact": "new@example.com",
            },
        ]
    )
    monkeypatch.setattr(ec, "is_escalation_context_enabled", lambda: True)
    _stub_cache(monkeypatch, rows)

    got = await ec.aget_contact_on_file(client_id=cid, phone_number=phone)
    assert got == "new@example.com"


async def test_contact_on_file_is_none_when_no_row_carries_one(monkeypatch):
    """Rows written before customer_contact existed must not break the gate."""
    import json as _json

    cid, phone = _uniq("contact-absent"), "web_1234abcd-5678-90ef"
    rows = _json.dumps(
        [
            {
                "escalation_id": "e1",
                "category": "Delivery Query",
                "status": "unresolved",
                "raised_at": (NOW - timedelta(hours=5)).isoformat(),
            }
        ]
    )
    monkeypatch.setattr(ec, "is_escalation_context_enabled", lambda: True)
    _stub_cache(monkeypatch, rows)

    assert await ec.aget_contact_on_file(client_id=cid, phone_number=phone) is None


async def test_contact_on_file_is_none_when_feature_is_off(monkeypatch):
    """Hard-off must keep the gate byte-identical to its pre-change behaviour."""

    def _explode(*a, **k):
        raise AssertionError("no I/O expected when the feature is disabled")

    monkeypatch.setattr(ec, "is_escalation_context_enabled", lambda: False)
    monkeypatch.setattr(ec, "_redis_get", _explode)
    monkeypatch.setattr(ec, "_load_rows_json", _explode)

    assert (
        await ec.aget_contact_on_file(
            client_id=_uniq("contact-off"), phone_number="web_abcd1234-0000-1111"
        )
        is None
    )


# ── the contact-collection gate itself ─────────────────────────────────────


def _msg(text):
    class _M:
        type = "human"
        content = text

    return _M()


async def test_gate_still_asks_when_nothing_is_on_file(monkeypatch):
    """The lookup must not weaken the existing ask — only skip it when moot."""
    import fashion_bot.utils.escalation_context as ecm
    from fashion_bot.utils import phone_number_utils as pnu

    monkeypatch.setattr(ecm, "aget_contact_on_file", lambda **kw: _async(None))
    state = {"phone_number": "web_02095d1c-2e07-4d01", "messages": [], "scratchpad": ""}

    ask, contact = await pnu.aresolve_contact_collection_gate(state, client_id="c1")
    assert ask is True and contact is None
    assert "escalation_phone_requested" in state["scratchpad"], "flag must be set"


async def test_gate_does_not_ask_again_when_a_contact_is_on_file(monkeypatch):
    """The fix: a returning guest is not re-asked for an address already recorded."""
    import fashion_bot.utils.escalation_context as ecm
    from fashion_bot.utils import phone_number_utils as pnu

    monkeypatch.setattr(ecm, "aget_contact_on_file", lambda **kw: _async("asha@example.com"))
    state = {"phone_number": "web_02095d1c-2e07-4d01", "messages": [], "scratchpad": ""}

    ask, contact = await pnu.aresolve_contact_collection_gate(state, client_id="c1")
    assert ask is False
    assert contact == "asha@example.com"
    assert not state["scratchpad"], "no need to record an ask that never happened"


async def test_typed_contact_beats_the_one_on_file(monkeypatch):
    """A customer correcting their address must not be overridden by history."""
    import fashion_bot.utils.escalation_context as ecm
    from fashion_bot.utils import phone_number_utils as pnu

    monkeypatch.setattr(ecm, "aget_contact_on_file", lambda **kw: _async("old@example.com"))
    state = {
        "phone_number": "web_02095d1c-2e07-4d01",
        "messages": [_msg("actually use new@example.com")],
        "scratchpad": "",
    }

    ask, contact = await pnu.aresolve_contact_collection_gate(state, client_id="c1")
    assert ask is False and contact == "new@example.com"


async def test_gate_second_attempt_is_unchanged(monkeypatch):
    """Flag already set ⇒ take what they typed, and never consult the snapshot."""
    import fashion_bot.utils.escalation_context as ecm
    from fashion_bot.utils import phone_number_utils as pnu

    def _explode(**kw):
        raise AssertionError("snapshot must not be read once the ask has happened")

    monkeypatch.setattr(ecm, "aget_contact_on_file", _explode)
    state = {
        "phone_number": "web_02095d1c-2e07-4d01",
        "messages": [_msg("asha@example.com")],
        "scratchpad": '{"escalation_phone_requested": true}',
    }

    ask, contact = await pnu.aresolve_contact_collection_gate(state, client_id="c1")
    assert ask is False and contact == "asha@example.com"


async def test_gate_is_inert_for_a_real_phone_number(monkeypatch):
    """WhatsApp customers were never gated and must stay that way — no I/O."""
    from fashion_bot.utils import phone_number_utils as pnu
    import fashion_bot.utils.escalation_context as ecm

    def _explode(**kw):
        raise AssertionError("no lookup for a customer who already has a phone")

    monkeypatch.setattr(ecm, "aget_contact_on_file", _explode)
    ask, contact = await pnu.aresolve_contact_collection_gate(
        {"phone_number": "919876543210", "messages": [], "scratchpad": ""}, client_id="c1"
    )
    assert ask is False and contact is None


# ── email as an identity key: reuniting a guest across devices ─────────────


class _FakeCur:
    """Minimal async cursor recording the statements the linker issues."""

    def __init__(self, select_rows, rowcount=0):
        self._select_rows = select_rows
        self.rowcount = rowcount
        self.executed = []
        self._last = ""

    async def execute(self, sql, params=None):
        self._last = " ".join(sql.split())
        self.executed.append((self._last, params))

    async def fetchall(self):
        return self._select_rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _stub_db(monkeypatch, cur):
    from fashion_bot import database_manager as dbm

    class _Conn:
        def cursor(self):
            return cur

        async def commit(self):
            pass

    class _CM:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(dbm, "get_async_postgres_connection", lambda: _CM())


async def test_email_link_moves_guest_rows_and_busts_both_caches(monkeypatch):
    """The gap-2 fix: an email typed on a new device reunites the old escalations."""
    monkeypatch.setattr(ec, "is_escalation_context_enabled", lambda: True)
    _patch_client_flag(monkeypatch)
    cur = _FakeCur([{"customer_phone": "web_aaaaaaaa-1111-22"}], rowcount=2)
    _stub_db(monkeypatch, cur)

    busted = []
    monkeypatch.setattr(
        ec, "abust_escalation_snapshot", lambda c, p: (busted.append(p), _async(None))[1]
    )

    moved = await ec.alink_escalations_by_contact(
        client_id="11111111-1111-1111-1111-111111111111",
        contact="Asha@Example.com",
        identity="web_bbbbbbbb-3333-4444-5555-666677778888",
    )
    assert moved == 2

    select_sql, select_params = cur.executed[0]
    assert "client_id = %s::uuid" in select_sql, "tenant-scoped (AGENTS.md §7)"
    assert "asha@example.com" in select_params, "compared lowercased"
    assert "LEFT(customer_phone, 4) = ANY(%s)" in select_sql, (
        "a row already keyed to a real phone must never be downgraded to a session id"
    )
    update_sql, update_params = cur.executed[1]
    assert update_sql.startswith("UPDATE escalations")
    assert update_params[0] == "web_bbbbbbbb-3333-44", "written truncated to VARCHAR(20)"
    assert len(update_params[0]) == 20
    # both sides of the move are stale: the source key still lists rows that
    # left, the target may hold a long-lived "no escalations" answer
    assert "web_aaaaaaaa-1111-22" in busted
    assert any(str(b).startswith("web_bbbbbbbb") for b in busted)


async def test_email_link_is_a_no_op_when_there_is_nothing_to_move(monkeypatch):
    """The common case must not issue a write."""
    monkeypatch.setattr(ec, "is_escalation_context_enabled", lambda: True)
    _patch_client_flag(monkeypatch)
    cur = _FakeCur([])
    _stub_db(monkeypatch, cur)

    moved = await ec.alink_escalations_by_contact(
        client_id="11111111-1111-1111-1111-111111111111",
        contact="nobody@example.com",
        identity="web_bbbbbbbb-3333-4444",
    )
    assert moved == 0
    assert not any(s.startswith("UPDATE") for s, _ in cur.executed), "no blind write"


async def test_email_link_is_idempotent(monkeypatch):
    """Re-running finds nothing left to move — the rows already carry the target."""
    monkeypatch.setattr(ec, "is_escalation_context_enabled", lambda: True)
    _patch_client_flag(monkeypatch)
    cur = _FakeCur([])
    _stub_db(monkeypatch, cur)

    args = dict(
        client_id="11111111-1111-1111-1111-111111111111",
        contact="asha@example.com",
        identity="web_bbbbbbbb-3333-4444",
    )
    assert await ec.alink_escalations_by_contact(**args) == 0
    assert await ec.alink_escalations_by_contact(**args) == 0


async def test_email_link_refuses_a_non_email_and_a_real_phone(monkeypatch):
    """Only an email, and only onto a guest identity — no I/O otherwise."""
    monkeypatch.setattr(ec, "is_escalation_context_enabled", lambda: True)
    _patch_client_flag(monkeypatch)

    from fashion_bot import database_manager as dbm

    def _explode():
        raise AssertionError("no database work expected")

    monkeypatch.setattr(dbm, "get_async_postgres_connection", _explode)

    # a phone number is not an email — that path is the session migration's job
    assert (
        await ec.alink_escalations_by_contact(
            client_id="c1", contact="9876543210", identity="web_aaaa-1111"
        )
        == 0
    )
    # never re-key onto a real phone: the phone is the stronger identity
    assert (
        await ec.alink_escalations_by_contact(
            client_id="c1", contact="asha@example.com", identity="919876543210"
        )
        == 0
    )


async def test_email_link_does_nothing_when_the_feature_is_off(monkeypatch):
    """Hard-off must mean no reads, no writes, no behaviour change at all."""
    monkeypatch.setattr(ec, "is_escalation_context_enabled", lambda: False)

    from fashion_bot import config_manager, database_manager as dbm

    def _explode(*a, **k):
        raise AssertionError("no I/O expected when the feature is disabled")

    monkeypatch.setattr(config_manager, "aget_config", _explode)
    monkeypatch.setattr(dbm, "get_async_postgres_connection", _explode)

    assert (
        await ec.alink_escalations_by_contact(
            client_id="c1", contact="asha@example.com", identity="web_aaaa-1111"
        )
        == 0
    )


async def test_email_link_without_client_id_is_an_error_not_a_default(monkeypatch):
    """An unscoped UPDATE would hand one tenant's escalations to another."""
    monkeypatch.setattr(ec, "is_escalation_context_enabled", lambda: True)

    reported = []
    import fashion_bot.rollbar_config as rc

    monkeypatch.setattr(rc, "report_error", lambda msg, **kw: reported.append(msg))

    from fashion_bot import database_manager as dbm

    monkeypatch.setattr(
        dbm,
        "get_async_postgres_connection",
        lambda: (_ for _ in ()).throw(AssertionError("must not query unscoped")),
    )

    moved = await ec.alink_escalations_by_contact(
        client_id=None, contact="asha@example.com", identity="web_aaaa-1111"
    )
    assert moved == 0
    assert reported, "a missing client_id must be reported, not silently skipped"


async def test_email_link_fails_open_on_a_database_error(monkeypatch):
    """A convenience feature must never break a customer's turn."""
    monkeypatch.setattr(ec, "is_escalation_context_enabled", lambda: True)
    _patch_client_flag(monkeypatch)

    from fashion_bot import database_manager as dbm

    class _CM:
        async def __aenter__(self):
            raise RuntimeError("pool exhausted")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(dbm, "get_async_postgres_connection", lambda: _CM())

    moved = await ec.alink_escalations_by_contact(
        client_id="11111111-1111-1111-1111-111111111111",
        contact="asha@example.com",
        identity="web_aaaa-1111",
    )
    assert moved == 0


async def test_email_link_refuses_a_contact_shared_by_many_identities(monkeypatch):
    """A store's own support address is not an identity — merging on it leaks."""
    monkeypatch.setattr(ec, "is_escalation_context_enabled", lambda: True)
    _patch_client_flag(monkeypatch)
    cur = _FakeCur(
        [{"customer_phone": f"web_{i:016d}"} for i in range(ec._MAX_CONTACT_LINK_IDENTITIES + 1)],
        rowcount=9,
    )
    _stub_db(monkeypatch, cur)

    moved = await ec.alink_escalations_by_contact(
        client_id="11111111-1111-1111-1111-111111111111",
        contact="support@theshop.com",
        identity="web_bbbbbbbb-3333-4444",
    )
    assert moved == 0
    assert not any(s.startswith("UPDATE") for s, _ in cur.executed), (
        "one customer must never inherit another's escalations"
    )


async def test_gate_uses_the_caller_s_phone_not_the_one_in_state(monkeypatch):
    """escalate_to_agent takes phone_number as a TOOL ARGUMENT.

    It falls back to state only when the LLM omits it, so the gate has always
    judged the effective value — e.g. a real number the model passed while the
    web session id was still the state phone skipped the gate entirely. Deriving
    the phone from state inside the helper would start asking those customers
    for a contact they had already been recognised by.
    """
    import fashion_bot.utils.escalation_context as ecm
    from fashion_bot.utils import phone_number_utils as pnu

    def _explode(**kw):
        raise AssertionError("a real phone number must not reach the snapshot")

    monkeypatch.setattr(ecm, "aget_contact_on_file", _explode)
    state = {"phone_number": "web_02095d1c-2e07-4d01", "messages": [], "scratchpad": ""}

    ask, contact = await pnu.aresolve_contact_collection_gate(
        state, phone_number="9876543210", client_id="c1"
    )
    assert ask is False and contact is None


async def test_gate_treats_a_placeholder_phone_as_no_phone(monkeypatch):
    """The restock flow defaults a missing phone to the literal 'Not provided'.

    That is truthy and not a real number, so it has always engaged the gate.
    Falling back to ``state.get("phone_number")`` — which is absent here — would
    return None and skip the ask instead.
    """
    import fashion_bot.utils.escalation_context as ecm
    from fashion_bot.utils import phone_number_utils as pnu

    monkeypatch.setattr(ecm, "aget_contact_on_file", lambda **kw: _async(None))
    state = {"messages": [], "scratchpad": ""}

    ask, _ = await pnu.aresolve_contact_collection_gate(
        state, phone_number="Not provided", client_id="c1"
    )
    assert ask is True, "a placeholder must still prompt for a contact"


async def test_gate_without_state_still_asks(monkeypatch):
    """No state means the flag cannot be recorded, so the ask must still happen."""
    import fashion_bot.utils.escalation_context as ecm
    from fashion_bot.utils import phone_number_utils as pnu

    monkeypatch.setattr(ecm, "aget_contact_on_file", lambda **kw: _async(None))

    ask, contact = await pnu.aresolve_contact_collection_gate(
        None, phone_number="web_02095d1c-2e07-4d01", client_id="c1"
    )
    assert ask is True and contact is None


# ── resolution must survive threading and truncation ───────────────────────
#
# Regression suite for the staging incident of 09-Aug-2026 (LangSmith trace
# 019fe64f…): a support agent resolved the customer's issue at 16:41 IST with
# "Talked to the customer. He is satisfied now."; at 17:06 the customer asked
# "What is the status of my complaint?" and the bot apologised for the delay and
# re-escalated. The snapshot HAD both resolved rows — record_count 15,
# unresolved_count 13 — and the block still said UNRESOLVED.


def _res(eid, category, raised_iso, *, status="unresolved", order_id=None,
         resolved_iso=None, resolution_text=None):
    return ec.EscalationRecord(
        escalation_id=eid,
        category=category,
        status=status,
        raised_at=datetime.fromisoformat(raised_iso).replace(tzinfo=timezone.utc),
        resolved_at=(
            datetime.fromisoformat(resolved_iso).replace(tzinfo=timezone.utc)
            if resolved_iso else None
        ),
        resolution_text=resolution_text,
        reason=None,
        order_id=order_id,
    )


_INCIDENT_RESOLUTION = "Talked to the customer. He is satisfied now."


def _incident_records():
    """The exact 15 rows the production snapshot returned at 11:36:43Z."""
    rows = [
        _res("f1", "Frustration", "2026-08-01T10:24:08"),
        _res("c1", "Cancellation Requests", "2026-08-01T11:47:37", order_id="gv17408"),
        _res("c2", "Cancellation Requests", "2026-08-01T14:44:06", order_id="gv17408"),
        _res("r1", "Restocking Query", "2026-08-01T14:53:12"),
        _res("f2", "Frustration", "2026-08-01T15:01:46"),
        _res("f3", "Frustration", "2026-08-01T16:34:37"),
        _res("f4", "Frustration", "2026-08-01T16:37:32"),
        _res("b1", "Bulk Order Discount", "2026-08-01T16:48:42"),
        _res("c3", "Cancellation Requests", "2026-08-02T05:57:42", order_id="gv17408"),
        _res("f5", "Frustration", "2026-08-02T06:01:11"),
        _res("f6", "Frustration", "2026-08-02T09:41:17"),
        _res("g1", "General", "2026-08-04T12:13:32"),
        _res("f7", "Frustration", "2026-08-04T12:14:13"),
        _res("f8", "Frustration", "2026-08-09T10:53:02", status="resolved",
             resolved_iso="2026-08-09T11:11:19", resolution_text=_INCIDENT_RESOLUTION),
        _res("f9", "Frustration", "2026-08-09T11:05:24", status="resolved",
             resolved_iso="2026-08-09T11:11:19", resolution_text=_INCIDENT_RESOLUTION),
    ]
    assert len(rows) == 15, "must match the production record_count"
    assert sum(1 for r in rows if r.is_unresolved) == 13, "must match unresolved_count"
    return rows


def test_incident_replay_surfaces_the_recorded_resolution(monkeypatch):
    """The production rows must now produce a block that states what the team did."""
    now = datetime(2026, 8, 9, 11, 36, 43, tzinfo=timezone.utc)

    block = ec.render_escalation_context_block(
        ec.build_threads(_incident_records(), now=now), now=now
    )
    assert _INCIDENT_RESOLUTION in block, (
        "the resolution a human recorded must reach the agent"
    )
    # And it must lead, because it is the newest thing that happened.
    assert block.index("RESOLVED by the support team") < block.index("UNRESOLVED")


def test_orderless_rows_split_on_a_multi_day_gap():
    """Eight months of 'Frustration' is not one issue."""
    rows = [
        _res("a", "Frustration", "2026-08-01T10:00:00"),
        _res("b", "Frustration", "2026-08-01T10:30:00"),
        _res("c", "Frustration", "2026-08-09T10:00:00"),
    ]
    threads = ec.build_threads(rows)
    assert len(threads) == 2, "a week's gap starts a new issue"
    assert {t.chase_count for t in threads} == {1, 0}


def test_orderless_rows_inside_the_window_stay_one_issue():
    """The two-day follow-up this feature exists for must not be split."""
    rows = [
        _res("a", "Frustration", "2026-08-01T10:00:00"),
        _res("b", "Frustration", "2026-08-03T09:00:00"),  # 47h later
    ]
    threads = ec.build_threads(rows)
    assert len(threads) == 1 and threads[0].chase_count == 1


def test_rows_sharing_an_order_never_split_however_far_apart():
    """An order id names ONE issue — distance is irrelevant."""
    rows = [
        _res("a", "Delivery Query", "2026-07-27T10:00:00", order_id="gv16384"),
        _res("b", "Frustration", "2026-08-09T10:00:00", order_id="gv16384"),
    ]
    threads = ec.build_threads(rows)
    assert len(threads) == 1, "13 days apart but the same order"


def test_gap_split_can_be_disabled(monkeypatch):
    monkeypatch.setattr(ec, "_THREAD_GAP_HOURS", 0)
    rows = [
        _res("a", "Frustration", "2026-07-27T10:00:00"),
        _res("b", "Frustration", "2026-08-09T10:00:00"),
    ]
    assert len(ec.build_threads(rows)) == 1


def test_partial_resolution_is_surfaced_not_discarded(monkeypatch):
    """A resolved row inside a still-open thread must still report what was done.

    Before this, ``resolution_text = None if unresolved`` threw the recorded
    action away, so the agent said "no update yet" over the top of it.
    """
    now = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)
    rows = [
        _res("open", "Delivery Query", "2026-08-08T10:00:00", order_id="gv16384"),
        _res("done", "Delivery Query", "2026-08-08T11:00:00", order_id="gv16384",
             status="resolved", resolved_iso="2026-08-09T09:00:00",
             resolution_text="Refund issued to source account."),
    ]
    threads = ec.build_threads(rows, now=now)
    assert len(threads) == 1
    thread = threads[0]
    assert thread.is_unresolved, "an open member keeps the thread open"
    assert thread.has_partial_resolution
    assert thread.open_member_count == 1

    block = ec.render_escalation_context_block(threads, now=now)
    assert "Refund issued to source account." in block
    assert "PARTLY RESOLVED" in block
    assert "no human update recorded yet" not in block, (
        "must not claim there is no update when one is recorded"
    )


def test_a_fresh_resolution_is_never_truncated_away(monkeypatch):
    """It outranks stale open issues — it is what a status question is about."""
    now = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(ec, "_MAX_THREADS", 2)
    rows = [
        _res(f"old{i}", "Delivery Query", f"2026-08-0{i+1}T10:00:00", order_id=f"gv{i}")
        for i in range(4)
    ] + [
        _res("fresh", "Frustration", "2026-08-09T09:00:00", status="resolved",
             resolved_iso="2026-08-09T11:00:00", resolution_text="Sorted on a call."),
    ]
    block = ec.render_escalation_context_block(ec.build_threads(rows, now=now), now=now)
    assert "Sorted on a call." in block


def test_stale_resolved_still_ranks_below_unresolved(monkeypatch):
    """The original guarantee holds: old resolved history never evicts an open issue."""
    now = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(ec, "_MAX_THREADS", 1)
    rows = [
        _res("open", "Delivery Query", "2026-07-30T10:00:00", order_id="gv1"),
        _res("done", "Restocking Query", "2026-08-05T10:00:00", order_id="gv2",
             status="resolved", resolved_iso="2026-08-05T12:00:00",
             resolution_text="back in stock"),
    ]
    block = ec.render_escalation_context_block(ec.build_threads(rows, now=now), now=now)
    assert "UNRESOLVED" in block and "back in stock" not in block


def test_split_threads_get_distinct_keys():
    """Thread-keyed state (the alert cooldown) must not collide across issues."""
    rows = [
        _res("a", "Frustration", "2026-08-01T10:00:00"),
        _res("b", "Frustration", "2026-08-09T10:00:00"),
    ]
    keys = {t.thread_key for t in ec.build_threads(rows)}
    assert len(keys) == 2


def test_lineage_still_matches_a_split_category_thread():
    """Gap-splitting must not break follow-up lineage on order-less issues.

    The split keys carry a "#<root>" suffix; matching on the unsuffixed key
    would find nothing, so a chase would attach no lineage and never key the
    alert cooldown.
    """
    now = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)
    rows = [
        _res("old", "Frustration", "2026-07-28T10:00:00"),
        _res("new", "Frustration", "2026-08-08T10:00:00"),
    ]
    threads = ec.build_threads(rows, now=now)
    assert len(threads) == 2, "precondition: the gap split them"

    match = ec.find_matching_thread(threads, category="Frustration")
    assert match is not None, "a chase must still find its issue"
    # The newest open run — what the customer is chasing now.
    assert match.root_escalation_id == "new"


def test_lineage_on_an_order_is_unaffected_by_splitting():
    now = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)
    rows = [
        _res("a", "Delivery Query", "2026-07-28T10:00:00", order_id="gv16384"),
        _res("b", "Frustration", "2026-08-08T10:00:00", order_id="gv16384"),
    ]
    threads = ec.build_threads(rows, now=now)
    match = ec.find_matching_thread(threads, order_id="gv16384")
    assert match is not None and match.root_escalation_id == "a"


def test_gap_split_without_the_recency_tier_would_regress(monkeypatch):
    """Pins the coupling between the two knobs.

    Splitting is what lets a run resolve cleanly; the recency tier is what keeps
    that clean resolution from being truncated away. Turning the tier off while
    splitting is on is worse than either alone, so the default must never ship
    that combination.
    """
    now = datetime(2026, 8, 9, 11, 36, 43, tzinfo=timezone.utc)
    # Read the shipped defaults BEFORE patching: they are what must not have the hole.
    shipped_recent, shipped_gap = ec._RECENT_RESOLUTION_HOURS, ec._THREAD_GAP_HOURS
    assert shipped_recent > 0 or shipped_gap == 0, "the defaults must not ship the hole"

    monkeypatch.setattr(ec, "_RECENT_RESOLUTION_HOURS", 0)
    block = ec.render_escalation_context_block(
        ec.build_threads(_incident_records(), now=now), now=now
    )
    assert _INCIDENT_RESOLUTION not in block, (
        "if this starts passing the coupling is gone and the comment is stale"
    )


def test_instructions_do_not_route_a_vague_question_past_a_resolution():
    """The routing rule must not steer the agent off a just-resolved [E1].

    Staging trace 019fe67b: the block correctly led with RESOLVED + the action
    taken, and the bot still apologised for the delay and claimed to re-escalate.
    The model was obedient — step 1 said "no order named -> the most recent
    UNRESOLVED issue", and "Any update on my complaint?" names no order. The
    block was right and the instruction sent the agent past it.
    """
    text = ec._HOW_TO_USE
    assert "the most recent UNRESOLVED issue" not in text, (
        "this rule routes a vague status question away from a fresh resolution"
    )
    assert "[E1] is the default answer" in text
    # The vague-status-question case is called out by name, because that is the
    # exact phrasing that broke it.
    assert "any update?" in text


def test_a_vague_status_question_lands_on_the_resolved_entry(monkeypatch):
    """End-to-end on the production rows: [E1] must be the resolved thread."""
    now = datetime(2026, 8, 9, 12, 25, 11, tzinfo=timezone.utc)
    rows = _incident_records()
    block = ec.render_escalation_context_block(
        ec.build_threads(rows, now=now), now=now
    )
    first_entry = block[block.index("[E1]"):block.index("[E2]")]
    assert "RESOLVED by the support team" in first_entry
    assert _INCIDENT_RESOLUTION in first_entry


def test_block_forbids_claiming_an_escalation_that_never_happened():
    """Staging trace 019fe67b contains no escalate_to_agent call at all.

    The reply still told the customer "I have flagged your request again as a
    priority for our support team". The tool was loaded and available; the model
    simply narrated an action it never took, which is a promise to a waiting
    customer that nothing on our side will honour.
    """
    text = ec._HOW_TO_USE
    assert "NEVER claim an action you did not take" in text
    assert "escalate_to_agent on THIS turn" in text


# ── the block must survive the agent's own re-escalation rule ──────────────


def test_an_open_issue_is_never_truncated_away_by_fresh_resolutions(monkeypatch):
    """Mirror image of the bug the recency tier fixed.

    Trace 019fe689: five issues resolved within the hour and one still open —
    every slot went to a resolved entry and the customer's outstanding problem
    vanished from the block entirely.
    """
    now = datetime(2026, 8, 9, 12, 39, 54, tzinfo=timezone.utc)
    monkeypatch.setattr(ec, "_MAX_THREADS", 3)
    rows = [
        _res("open", "Bulk Order Discount", "2026-08-01T16:48:42"),
    ] + [
        _res(f"done{i}", f"Cat{i}", f"2026-08-0{i+1}T10:00:00", order_id=f"gv{i}",
             status="resolved", resolved_iso="2026-08-09T12:29:00",
             resolution_text="handled")
        for i in range(5)
    ]
    block = ec.render_escalation_context_block(
        ec.build_threads(rows, now=now), now=now
    )
    assert "Bulk Order Discount" in block, (
        "an open issue must keep a slot however many resolutions are fresher"
    )
    assert "UNRESOLVED" in block


def test_a_closed_lead_entry_overrides_the_re_escalation_rule():
    """The escalation_handler prompt says: prior promise + still asking => escalate.

    That clause is emphatic, sits in the agent's role prompt ahead of this block,
    and has no notion of the issue having since been closed. Trace 019fe689 is
    what it produces: an apology and a fresh ticket on a complaint the team had
    closed fifteen minutes earlier. The block has to contradict it by name.
    """
    now = datetime(2026, 8, 9, 12, 39, 54, tzinfo=timezone.utc)
    rows = [
        _res("done", "Frustration", "2026-08-09T10:53:02", status="resolved",
             resolved_iso="2026-08-09T12:24:27",
             resolution_text="Talked to the customer. He is satisfied now."),
    ]
    block = ec.render_escalation_context_block(
        ec.build_threads(rows, now=now), now=now
    )
    assert "[E1] IS CLOSED" in block
    assert "OVERRIDES ANY STANDING RULE ABOUT RE-ESCALATING" in block
    assert "do NOT re-escalate it" in block
    # Nothing open at all => the strongest form, forbidding the tool outright.
    assert "do NOT call escalate_to_agent at all" in block
    # The directive must come BEFORE the entries, so it is read first.
    assert block.index("[E1] IS CLOSED") < block.index("STATUS: RESOLVED")


def test_a_closed_lead_still_permits_acting_on_a_genuinely_open_entry():
    """Must not become a blanket gag when something else really is open."""
    now = datetime(2026, 8, 9, 12, 39, 54, tzinfo=timezone.utc)
    rows = [
        _res("done", "Frustration", "2026-08-09T10:53:02", status="resolved",
             resolved_iso="2026-08-09T12:24:27", resolution_text="sorted"),
        _res("open", "Bulk Order Discount", "2026-08-01T16:48:42"),
    ]
    block = ec.render_escalation_context_block(
        ec.build_threads(rows, now=now), now=now
    )
    assert "[E1] IS CLOSED" in block
    assert "do NOT call escalate_to_agent at all" not in block
    assert "Another entry below is still open" in block


def test_no_closed_lead_directive_when_the_lead_is_open():
    """An ordinary open follow-up must read exactly as before."""
    now = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)
    rows = [_res("open", "Delivery Query", "2026-08-07T10:00:00", order_id="gv1")]
    block = ec.render_escalation_context_block(
        ec.build_threads(rows, now=now), now=now
    )
    assert "IS CLOSED" not in block


# ── dashboard progress updates ("Add Progress Update" -> comments[]) ───────
#
# The ops dashboard records a human action in TWO places and only one of them
# touches `status`. Production trace 019fe6a7 reported unresolved_count: 3 while
# escalation #30D002FF carried resolution_status='in_progress' and a comment
# saying the product ships at 7pm — posted three minutes after it was raised.
# The block told the agent "no human update recorded yet".


def _noted(eid, category, raised_iso, note, note_iso, *, order_id=None,
           resolution_status="in_progress"):
    return ec.EscalationRecord(
        escalation_id=eid, category=category, status="unresolved",
        raised_at=datetime.fromisoformat(raised_iso).replace(tzinfo=timezone.utc),
        resolved_at=None, resolution_text=None, reason=None, order_id=order_id,
        resolution_status=resolution_status, progress_note=note,
        progress_note_at=datetime.fromisoformat(note_iso).replace(tzinfo=timezone.utc),
    )


_PROD_NOTE = "The product will be dispatched on august 9 at 7pm."


def test_progress_note_reaches_the_block_and_replaces_the_false_line():
    now = datetime(2026, 8, 9, 13, 13, 8, tzinfo=timezone.utc)
    rows = [_noted("30d002ff", "Delivery Query", "2026-08-09T11:14:27",
                   _PROD_NOTE, "2026-08-09T11:17:45", order_id="gv15790")]
    block = ec.render_escalation_context_block(ec.build_threads(rows, now=now), now=now)
    assert _PROD_NOTE in block
    assert "STATUS: OPEN — the team is working on it" in block
    assert "no human update recorded yet" not in block, (
        "an update IS recorded; saying otherwise is what broke this"
    )


def test_a_progress_note_does_not_pretend_the_issue_is_resolved():
    """in_progress is not resolved — the thread must stay open and re-escalatable."""
    now = datetime(2026, 8, 9, 13, 13, 8, tzinfo=timezone.utc)
    rows = [_noted("a", "Delivery Query", "2026-08-09T11:14:27",
                   _PROD_NOTE, "2026-08-09T11:17:45", order_id="gv1")]
    thread = ec.build_threads(rows, now=now)[0]
    assert thread.is_unresolved and thread.has_progress_note
    block = ec.render_escalation_context_block([thread], now=now)
    assert "RESOLVED by the support team" not in block
    assert "[E1] IS CLOSED" not in block, "a note must not trigger the closed-lead override"


def test_a_freshly_noted_issue_outranks_untouched_older_ones():
    """A teammate updating an issue ten minutes ago makes it the likely subject."""
    now = datetime(2026, 8, 9, 13, 13, 8, tzinfo=timezone.utc)
    rows = [
        _res("old1", "Product Complaint", "2026-08-01T04:33:24"),
        _res("old2", "Frustration", "2026-08-05T13:45:18"),
        _noted("new", "Delivery Query", "2026-08-09T11:14:27",
               _PROD_NOTE, "2026-08-09T11:17:45", order_id="gv15790"),
    ]
    block = ec.render_escalation_context_block(ec.build_threads(rows, now=now), now=now)
    assert block.index("Delivery Query") < block.index("Product Complaint")


def test_latest_comment_is_chosen_and_the_staff_email_is_dropped():
    """user_email names a staff member; the block forbids revealing those."""
    text, at = ec._latest_comment([
        {"text": "older", "created_at": "2026-08-09T10:00:00Z", "user_email": "a@x.com"},
        {"text": "newest", "created_at": "2026-08-09T11:17:45Z", "user_email": "b@x.com"},
    ])
    assert text == "newest"
    assert at is not None and at.hour == 11

    rows = [_noted("a", "Delivery Query", "2026-08-09T11:14:27",
                   "newest", "2026-08-09T11:17:45")]
    now = datetime(2026, 8, 9, 13, 13, 8, tzinfo=timezone.utc)
    block = ec.render_escalation_context_block(ec.build_threads(rows, now=now), now=now)
    assert "@x.com" not in block


def test_latest_comment_tolerates_junk():
    assert ec._latest_comment(None) == (None, None)
    assert ec._latest_comment([]) == (None, None)
    assert ec._latest_comment("not json") == (None, None)
    assert ec._latest_comment([{"no_text": 1}, "string", None]) == (None, None)
    # A note with no timestamp is still better than silence.
    text, at = ec._latest_comment([{"text": "undated"}])
    assert text == "undated" and at is None


def test_records_cached_before_progress_notes_existed_still_load():
    """Backward compatibility for payloads already in Redis."""
    import json as _json
    raw = _json.dumps([{
        "escalation_id": "e1", "category": "Delivery Query", "status": "unresolved",
        "raised_at": "2026-08-08T10:00:00+00:00",
    }])
    records = ec._records_from_json(raw)
    assert len(records) == 1
    assert records[0].progress_note is None and records[0].resolution_status is None


async def test_get_escalations_exposes_the_team_update(monkeypatch):
    """The tool and the block must never tell the agent different things."""
    import json as _json
    cid, phone = _uniq("team-update"), "919716336096"
    rows = _json.dumps([{
        "escalation_id": "30d002ff", "category": "Delivery Query", "status": "unresolved",
        "raised_at": "2026-08-09T11:14:27+00:00", "order_id": "gv15790",
        "progress_note": _PROD_NOTE, "progress_note_at": "2026-08-09T11:17:45+00:00",
        "resolution_status": "in_progress",
    }])
    monkeypatch.setattr(ec, "is_escalation_context_enabled", lambda: True)
    _stub_cache(monkeypatch, rows)
    monkeypatch.setattr(ec, "_load_human_replies_json", lambda c, v, s: _async("[]"))

    out = await ec.aget_escalation_status(client_id=cid, phone_number=phone)
    assert out["found"] is True
    issue = out["issues"][0]
    assert issue["team_update"] == _PROD_NOTE
    assert issue["team_update_at"]
    assert issue["status"] == "unresolved", "a note is not a resolution"
    assert "superadmin" not in _json.dumps(out)

"""FinOps dashboard (§4.8). Run: ``streamlit run dashboard/app.py`` with the API up (``finops serve``)."""

from __future__ import annotations

import os

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

API = os.environ.get("FINOPS_API_URL", "http://127.0.0.1:8000")
SERIES_1, SERIES_2 = "#2a78d6", "#eb6834"  # categorical slots 1–2 (validated palette)
INR = "₹"

st.set_page_config(page_title="FinOps Right-Sizing", page_icon="💸", layout="wide")


# ------------------------------------------------------------------ API helpers
def get(path: str, **params):
    try:
        r = requests.get(f"{API}{path}", params={k: v for k, v in params.items() if v not in (None, "", "All")},
                         timeout=30)  # fmt: skip
        r.raise_for_status()
        return r.json()
    except requests.RequestException as exc:
        st.error(f"API error on {path}: {exc}. Is `finops serve` running at {API}?")
        st.stop()


def post(path: str, json=None, **params):
    r = requests.post(f"{API}{path}", json=json, params=params, timeout=600)
    if r.status_code >= 400:
        st.error(r.json().get("detail", r.text))
        return None
    return r.json()


def bar(data: dict[str, float], title: str, money: bool = True, rate: float = 1.0) -> go.Figure:
    items = sorted(data.items(), key=lambda kv: kv[1])
    labels = [k for k, _ in items]
    values = [v * rate if money else v for _, v in items]
    fmt = f"{INR}%{{x:,.0f}}" if money else "%{x:,}"
    fig = go.Figure(
        go.Bar(
            x=values,
            y=labels,
            orientation="h",
            marker=dict(color=SERIES_1, cornerradius=4),
            hovertemplate=f"%{{y}}: {fmt}<extra></extra>",
            texttemplate=fmt,
            textposition="outside",
            cliponaxis=False,
        )  # fmt: skip
    )
    fig.update_layout(
        title=dict(text=title, font=dict(size=14)), height=60 + 44 * max(1, len(labels)), bargap=0.35,
        margin=dict(l=8, r=48, t=40, b=8), xaxis=dict(showgrid=True, zeroline=False, showticklabels=False),
        yaxis=dict(showgrid=False),
    )  # fmt: skip
    return fig


def money(usd: float, rate: float) -> str:
    return f"{INR}{usd * rate:,.0f}  (${usd:,.2f})"


# ------------------------------------------------------------------ pages
def page_overview():
    s = get("/summary")
    rate = s["usd_to_inr"]
    st.title("Overview")
    if not s.get("run"):
        st.info("No analysis yet. Trigger a run from the sidebar (or `finops collect && finops analyze`).")
        return
    billing = s.get("billing")
    c1, c2, c3, c4 = st.columns(4)
    if billing:
        c1.metric("Actual spend (Cost Explorer)", f"{INR}{billing['total'] * rate:,.0f}",
                  help=f"{billing['period_start'][:10]} → {billing['period_end'][:10]}, all services")  # fmt: skip
    else:
        c1.metric("Actual spend", "—", help="Cost Explorer not collected yet")
    c2.metric("Analysed run-rate / month", f"{INR}{s['total_current_monthly_inr']:,.0f}",
              help="On-demand cost of the in-scope EC2/EBS/RDS resources")  # fmt: skip
    c3.metric("Identified savings / month", f"{INR}{s['total_monthly_savings_inr']:,.0f}",
              help=f"${s['total_monthly_savings']:,.2f} USD")  # fmt: skip
    c4.metric("Waste", f"{s['waste_pct']:.1f}%", help="savings ÷ analysed run-rate")

    left, right = st.columns(2)
    if s["by_service"]:
        left.plotly_chart(bar(s["by_service"], "Savings by service (₹/month)", rate=rate), width="stretch")
    if s["by_risk_tier"]:
        right.plotly_chart(bar(s["by_risk_tier"], "Savings by risk tier (₹/month)", rate=rate),
                           width="stretch")  # fmt: skip
    left, right = st.columns(2)
    left.plotly_chart(bar(s["by_classification"], "Resources by classification", money=False),
                      width="stretch")  # fmt: skip
    if s["by_team"]:
        right.plotly_chart(bar(s["by_team"], "Savings by team tag (₹/month)", rate=rate), width="stretch")
    if billing and billing["by_service"]:
        top = dict(sorted(billing["by_service"].items(), key=lambda kv: -kv[1])[:8])
        st.plotly_chart(bar(top, "Actual spend by AWS service, top 8 (₹)", rate=rate), width="stretch")
    st.caption(f"Run #{s['run']['id']} finished {s['run']['finished_at']}. 1 USD = {INR}{rate}.")


def page_resources():
    st.title("Resources")
    c1, c2, c3 = st.columns(3)
    rtype = c1.selectbox("Type", ["All", "ec2", "ebs", "rds"])
    cls = c2.selectbox("Classification", ["All", "idle", "over_provisioned", "config", "right_sized",
                                          "insufficient_data"])  # fmt: skip
    tier = c3.selectbox("Risk tier", ["All", "safe_auto", "approval", "manual_only"])
    rows = get("/resources", type=rtype, classification=cls)
    if tier != "All":
        rows = [r for r in rows if r["risk_tier"] == tier]
    if not rows:
        st.info("No resources match.")
        return

    sparks: dict[str, list[float]] = {}
    for r in rows:
        if r["type"] in ("ec2", "rds"):
            sparks[r["id"]] = [p["p95"] for p in get(f"/resources/{r['id']}/metrics", days=14)]

    def spec_str(spec: dict | None) -> str:
        spec = spec or {}
        return spec.get("instance_type") or spec.get("instance_class") or (
            f"{spec.get('volume_type', '')} {spec.get('size_gb', '')} GB".strip() if "volume_type" in spec
            else spec.get("state", "")
        )  # fmt: skip

    df = pd.DataFrame([{
        "id": r["id"], "name": r["name"], "type": r["type"], "state": r["state"],
        "env": (r["tags"] or {}).get("env", ""), "classification": r["classification"],
        "risk tier": r["risk_tier"], "p95 CPU 14d": sparks.get(r["id"], []),
        "p95 CPU %": r["p95_cpu"], "current": spec_str(r["spec"]), "recommended": spec_str(r["recommended_spec"]),
        "₹/month now": (r["current_monthly_cost"] or 0) * st.session_state.rate,
        "₹/month saved": (r["monthly_savings"] or 0) * st.session_state.rate,
    } for r in rows])  # fmt: skip
    df = df.sort_values("₹/month saved", ascending=False)
    st.dataframe(
        df, hide_index=True, width="stretch",
        column_config={
            "p95 CPU 14d": st.column_config.LineChartColumn("p95 CPU (14 d)", y_min=0, y_max=100),
            "₹/month now": st.column_config.NumberColumn(format="₹%.0f"),
            "₹/month saved": st.column_config.NumberColumn(format="₹%.0f"),
            "p95 CPU %": st.column_config.NumberColumn(format="%.1f"),
        },
    )  # fmt: skip

    detail = st.selectbox("Inspect utilisation", [r["id"] for r in rows if r["type"] in ("ec2", "rds")] or ["—"])
    if detail != "—":
        pts = get(f"/resources/{detail}/metrics", metric="CPUUtilization", days=30)
        if pts:
            m = pd.DataFrame(pts)
            fig = go.Figure()
            fig.add_scatter(x=m["ts"], y=m["p95"], name="hourly p95", line=dict(color=SERIES_1, width=2))
            fig.add_scatter(x=m["ts"], y=m["max"], name="hourly max", line=dict(color=SERIES_2, width=2))
            fig.update_layout(
                title=dict(text=f"CPU utilisation — {detail} (%)", font=dict(size=14)), height=320,
                hovermode="x unified", margin=dict(l=8, r=8, t=40, b=8), yaxis=dict(range=[0, 100], title=None),
                legend=dict(orientation="h", y=1.12, x=1, xanchor="right"),
            )  # fmt: skip
            st.plotly_chart(fig, width="stretch")


def page_actions():
    st.title("Recommendations & actions")
    c1, c2, c3 = st.columns([1, 1, 2])
    tier = c1.selectbox("Risk tier", ["All", "safe_auto", "approval", "manual_only"])
    rtype = c2.selectbox("Type", ["All", "ec2", "ebs", "rds"])
    actor = c3.text_input("Your name (recorded in the audit log)", value=st.session_state.get("actor", ""))
    st.session_state.actor = actor
    recs = [r for r in get("/recommendations", risk_tier=tier, type=rtype) if r["action_type"]]
    if not recs:
        st.info("No actionable recommendations.")
    rate = st.session_state.rate
    for r in recs:
        header = (f"**{r['resource_id']}** · {r['resource_type']} · {r['classification']} → `{r['action_type']}` · "
                  f"saves **{INR}{r['monthly_savings'] * rate:,.0f}/mo** · {r['risk_tier']} · "
                  f"confidence {r['confidence']:.2f}")  # fmt: skip
        with st.expander(header):
            st.write(r["rationale"].get("rule", ""))
            a, b = st.columns(2)
            a.json({"current": r["current_spec"]}, expanded=False)
            b.json({"recommended": r["recommended_spec"]}, expanded=False)
            with st.popover("Full rationale"):
                st.json(r["rationale"])
            col1, col2, col3 = st.columns(3)
            if col1.button("Dry-run plan", key=f"plan-{r['id']}"):
                out = post(f"/recommendations/{r['id']}/plan")
                if out:
                    st.code("\n".join(f"{p.get('service', '')}.{p.get('call', p)}({p.get('args', '')})"
                                      for p in out["plan"]))  # fmt: skip
                    for reason in out.get("blocked_reasons", []):
                        st.warning(f"Would be blocked: {reason}")
            if col2.button("Approve", key=f"approve-{r['id']}", disabled=not actor):
                out = post(f"/recommendations/{r['id']}/approve", json={"approved_by": actor})
                if out:
                    (st.success if out["status"] == "approved" else st.warning)(
                        f"Action #{out['id']} is {out['status']}. " + "; ".join(out.get("blocked_reasons", []))
                    )

    st.subheader("Action log")
    acts = get("/actions")
    if not acts:
        st.caption("No actions yet.")
        return
    for a in acts:
        with st.expander(f"#{a['id']} · {a['resource_id']} · `{a['action_type']}` · **{a['status']}** · "
                         f"approved by {a['approved_by'] or '—'}"):  # fmt: skip
            st.code(a["log"] or "(empty)")
            if a["rollback"]:
                st.caption(f"Rollback: `{a['rollback']}`")
            if a["status"] in ("dry_run", "approved"):
                c1, c2 = st.columns(2)
                if c1.button("Dry-run execute", key=f"dry-{a['id']}"):
                    out = post(f"/actions/{a['id']}/execute", dry_run=True, actor=actor or "dashboard")
                    if out:
                        st.json(out["plan"])
                confirm = c2.checkbox("I understand this changes AWS resources", key=f"confirm-{a['id']}")
                if c2.button("Execute for real", key=f"exec-{a['id']}", disabled=not (confirm and actor),
                             type="primary"):  # fmt: skip
                    out = post(f"/actions/{a['id']}/execute", dry_run=False, actor=actor)
                    if out:
                        (st.success if out["status"] == "executed" else st.error)(f"Status: {out['status']}")


def page_validation():
    st.title("Validation")
    v = get("/validation")
    co = v["compute_optimizer"]
    st.subheader("Agreement with AWS Compute Optimizer")
    if not co.get("compared"):
        st.info("No overlapping Compute Optimizer findings yet. Opt in to Compute Optimizer in the account, "
                "wait ~12 h, then run `finops collect`.")  # fmt: skip
    else:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Resources compared", co["compared"])
        c2.metric("Agreement", f"{co['agreement_pct']}%")
        c3.metric("Precision", "—" if co["precision"] is None else f"{co['precision']:.2f}")
        c4.metric("Recall", "—" if co["recall"] is None else f"{co['recall']:.2f}")
        cm = co["confusion"]
        st.table(pd.DataFrame(
            {"CO flags waste": [cm["tp"], cm["fn"]], "CO says optimized": [cm["fp"], cm["tn"]]},
            index=["We flag waste", "We say right-sized"],
        ))  # fmt: skip
        if co.get("instance_type_match_pct") is not None:
            st.caption(f"Same target instance type when both recommend a resize: {co['instance_type_match_pct']}%")
        st.dataframe(pd.DataFrame(co["rows"]), hide_index=True, width="stretch")

    st.subheader("Predicted vs realised savings")
    real = v["realised"]
    rate = st.session_state.rate
    st.metric("Predicted savings of executed actions", f"{INR}{real['predicted_total'] * rate:,.0f}/month")
    if real["executed_actions"]:
        st.dataframe(pd.DataFrame(real["executed_actions"]), hide_index=True, width="stretch")
    if real["tagged_billing"]:
        b = pd.DataFrame(real["tagged_billing"])
        st.plotly_chart(
            bar(dict(zip(b["period_start"].str[:10], b["total"], strict=True)),
                "Spend on finops:managed resources by period (₹)", rate=rate),
            width="stretch",
        )  # fmt: skip
    else:
        st.caption("Run `python evaluation/realised_savings.py` after a billing period to record tagged spend.")


# ------------------------------------------------------------------ shell
summary = get("/summary")
st.session_state.rate = summary.get("usd_to_inr", 88.0)
with st.sidebar:
    st.header("FinOps")
    page = st.radio("Page", ["Overview", "Resources", "Recommendations & actions", "Validation"],
                    label_visibility="collapsed")  # fmt: skip
    st.divider()
    collect = st.checkbox("Collect from AWS first", value=True)
    if st.button("Run now"):
        out = post("/runs", collect=collect)
        if out:
            st.success("Run started — refresh in a minute.")
    runs = get("/runs", limit=5)
    for r in runs:
        st.caption(f"#{r['id']} {r['kind']} · {r['status']} · {(r['finished_at'] or 'running')[:16]}")

{"Overview": page_overview, "Resources": page_resources, "Recommendations & actions": page_actions,
 "Validation": page_validation}[page]()  # fmt: skip

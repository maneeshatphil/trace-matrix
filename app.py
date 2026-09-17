"""Streamlit dashboard for the automated traceability matrix."""

import io
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.append(str(ROOT_DIR))

from src.db import postgres_client as db
from src.pipeline import SUPPORTED_EXTENSIONS, run_pipeline, stage_uploads

st.set_page_config(page_title="Traceability Matrix", page_icon="🔗", layout="wide")

CACHE_TTL = 60
PAGES = ["Overview", "Traceability matrix", "Gap analysis", "Review queue", "Item explorer"]
STAGE_NAMES = ["Engine 1 · Ingestion", "Engine 2 · Chunking", "Engine 3 · Linking"]


@st.cache_data(ttl=CACHE_TTL)
def load_summary():
    return db.get_summary()


@st.cache_data(ttl=CACHE_TTL)
def load_links(match_types, statuses, search, min_confidence):
    return db.get_links(list(match_types), list(statuses), search, min_confidence)


@st.cache_data(ttl=CACHE_TTL)
def load_uncovered():
    return db.get_uncovered_requirements()


@st.cache_data(ttl=CACHE_TTL)
def load_unlinked(doc_type):
    return db.get_unlinked_sources(doc_type)


@st.cache_data(ttl=CACHE_TTL)
def load_coverage_by_doc():
    return db.get_coverage_by_document()


@st.cache_data(ttl=CACHE_TTL)
def load_breakdown():
    return db.get_match_type_breakdown()


@st.cache_data(ttl=CACHE_TTL)
def load_documents():
    return db.get_documents()


def pct(numerator: int, denominator: int) -> float:
    return round(100.0 * numerator / denominator, 1) if denominator else 0.0


def build_excel(sheets: dict) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
        for name, frame in sheets.items():
            sheet_name = name[:31]
            frame.to_excel(writer, sheet_name=sheet_name, index=False)
            worksheet = writer.sheets[sheet_name]
            for idx, column in enumerate(frame.columns):
                longest = frame[column].astype(str).str.len().max()
                width = min(60, max(12, int(longest if pd.notna(longest) else 12) + 2))
                worksheet.set_column(idx, idx, width)
    return buffer.getvalue()


# --- Pipeline orchestration UI ---


def init_state():
    defaults = {
        "page": "Overview",
        "pipeline_running": False,
        "pipeline_paths": [],
        "pipeline_report": None,
        "pipeline_error": "",
        "uploader_round": 0,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def overlay_html(current: str, done: list) -> str:
    steps = "".join(
        f"<li class='{'done' if name in done else ('active' if name == current else '')}'>{name}</li>"
        for name in STAGE_NAMES
    )
    return f"""
<style>
/* The sidebar outranks any z-index we can set from inside the main block, so it is hidden
   outright while the engines run; that also stops a mid-run click from interrupting them. */
[data-testid="stSidebar"], header[data-testid="stHeader"] {{ display: none !important; }}
#rtm-overlay {{
  position: fixed; inset: 0; z-index: 99999;
  background: rgba(10, 13, 20, 0.92);
  display: flex; flex-direction: column; align-items: center; justify-content: center;
}}
#rtm-overlay .rtm-spinner {{
  width: 76px; height: 76px; border-radius: 50%;
  border: 6px solid rgba(255, 255, 255, 0.14);
  border-top-color: #ff4b4b;
  animation: rtm-spin 0.9s linear infinite;
}}
@keyframes rtm-spin {{ to {{ transform: rotate(360deg); }} }}
#rtm-overlay .rtm-title {{ margin-top: 22px; color: #fafafa; font-size: 1.15rem; font-weight: 600; }}
#rtm-overlay ul {{ list-style: none; padding: 0; margin: 14px 0 0; color: rgba(250,250,250,0.45); }}
#rtm-overlay li {{ margin: 6px 0; font-size: 0.92rem; }}
#rtm-overlay li.active {{ color: #fafafa; }}
#rtm-overlay li.done {{ color: #2ecc71; }}
#rtm-overlay li.done::after {{ content: " ✓"; }}
</style>
<div id="rtm-overlay">
  <div class="rtm-spinner"></div>
  <div class="rtm-title">{current}</div>
  <ul>{steps}</ul>
</div>
"""


def execute_pending_pipeline():
    """Blocks the script run behind a full-screen overlay until Engine 3 finishes."""
    overlay = st.empty()
    completed: list = []
    overlay.markdown(overlay_html("Starting pipeline…", completed), unsafe_allow_html=True)

    def progress(stage: str, state: str, detail: str):
        if state == "done":
            completed.append(stage)
            label = f"{stage} complete"
        elif state == "retry":
            label = f"{stage} failed — retrying…"
        elif state == "failed":
            label = f"{stage} failed"
        else:
            label = f"{stage} running…"
        overlay.markdown(overlay_html(label, completed), unsafe_allow_html=True)

    report = None
    error = ""
    try:
        report = run_pipeline(st.session_state.pipeline_paths, progress)
    except Exception as exc:
        error = str(exc)

    overlay.empty()
    st.session_state.pipeline_running = False
    st.session_state.pipeline_report = report
    st.session_state.pipeline_error = error

    if report is not None and report.ok:
        st.cache_data.clear()
        # Applied on the next run: the radio bound to "page" is not instantiated yet there.
        st.session_state.pending_nav = "Traceability matrix"

    st.rerun()


def render_pipeline_notice():
    # Read-and-clear: the outcome is a one-shot notification, not persistent page state.
    error = st.session_state.pipeline_error
    report = st.session_state.pipeline_report
    st.session_state.pipeline_error = ""
    st.session_state.pipeline_report = None

    if error:
        st.error(f"Pipeline could not start: {error}", icon="🚨")
        return

    if report is None:
        return

    if report.ok:
        detail = " ".join(stage.detail for stage in report.stages if stage.detail)
        st.success(f"Pipeline finished. {detail}", icon="✅")
        if report.failed_inputs:
            st.warning(
                "Some inputs were skipped:\n\n"
                + "\n".join(f"- {item}" for item in report.failed_inputs),
                icon="⚠️",
            )
    else:
        st.error(
            f"Pipeline stopped at **{report.first_error()}**\n\n"
            f"The step was retried automatically and failed again.",
            icon="🚨",
        )
        if st.button("Retry pipeline", type="primary"):
            st.session_state.pipeline_running = True
            st.rerun()


def render_upload_panel():
    st.markdown("### Add documents")
    st.caption(
        "Upload requirement, design, risk or test documents. "
        "Test-evidence folders must be uploaded as a `.zip` archive."
    )

    uploads = st.file_uploader(
        "Requirement / design / risk / test files",
        type=SUPPORTED_EXTENSIONS,
        accept_multiple_files=True,
        key=f"uploader_{st.session_state.uploader_round}",
    )

    folder_path = st.text_input(
        "Or a folder path on this machine (optional)",
        placeholder=r"d:\trace-matrix\data\PR.SmartNavigator...",
    )

    has_input = bool(uploads) or bool(folder_path.strip())
    if st.button("Run pipeline", type="primary", disabled=not has_input):
        try:
            staged = stage_uploads(uploads, [folder_path] if folder_path.strip() else [])
        except Exception as exc:
            st.error(f"Could not stage the input: {exc}", icon="🚨")
            return

        st.session_state.pipeline_paths = [str(p) for p in staged]
        st.session_state.pipeline_report = None
        st.session_state.pipeline_error = ""
        st.session_state.pipeline_running = True
        st.session_state.uploader_round += 1
        st.rerun()


def render_overview():
    render_upload_panel()
    st.divider()

    st.subheader("Coverage overview")
    summary = load_summary()

    req_pct = pct(summary["requirements_covered"], summary["requirements"])
    src_pct = pct(summary["sources_linked"], summary["sources"])

    c1, c2, c3, c4 = st.columns(4)
    c1.metric(
        "Requirement coverage",
        f"{req_pct}%",
        f"{summary['requirements_covered']} of {summary['requirements']}",
    )
    c2.metric(
        "Source items linked",
        f"{src_pct}%",
        f"{summary['sources_linked']} of {summary['sources']}",
    )
    c3.metric("Total links", summary["total_links"])
    c4.metric("Pending review", summary["pending_review"])

    st.progress(min(req_pct / 100.0, 1.0), text=f"Requirements traced: {req_pct}%")

    left, right = st.columns(2)

    with left:
        st.markdown("**Coverage by document**")
        coverage = load_coverage_by_doc()
        if coverage.empty:
            st.info("No requirement documents ingested yet.")
        else:
            coverage = coverage.copy()
            coverage["coverage_%"] = coverage.apply(
                lambda r: pct(r["covered"], r["requirements"]), axis=1
            )
            st.dataframe(coverage, width="stretch", hide_index=True)
            st.bar_chart(coverage.set_index("document")["coverage_%"])

    with right:
        st.markdown("**Links by match type**")
        breakdown = load_breakdown()
        if breakdown.empty:
            st.info("No links generated yet. Run Engine 3.")
        else:
            st.dataframe(breakdown, width="stretch", hide_index=True)
            st.bar_chart(breakdown.set_index("match_type")["links"])

    st.markdown("**Ingested documents**")
    docs = load_documents()
    if docs.empty:
        st.info("No documents ingested yet. Run Engine 1.")
    else:
        st.dataframe(docs, width="stretch", hide_index=True)


def render_matrix():
    st.subheader("Traceability matrix")

    f1, f2, f3 = st.columns([2, 2, 3])
    match_types = f1.multiselect(
        "Match type",
        ["EXACT_REGEX", "SEMANTIC_HIGH", "SEMANTIC_SUGGESTED"],
        default=["EXACT_REGEX", "SEMANTIC_HIGH", "SEMANTIC_SUGGESTED"],
    )
    statuses = f2.multiselect(
        "Status", ["ACTIVE", "NEEDS_REVIEW", "REJECTED"], default=["ACTIVE", "NEEDS_REVIEW"]
    )
    search = f3.text_input("Search item ID", placeholder="e.g. PR.UniGuide.FR")
    min_conf = st.slider("Minimum confidence", 0.0, 1.0, 0.0, 0.05)

    links = load_links(tuple(match_types), tuple(statuses), search or None, min_conf)

    st.caption(f"{len(links)} link(s) matched.")
    if links.empty:
        st.info("No links match the current filters.")
        return

    st.dataframe(
        links.drop(columns=["id"]),
        width="stretch",
        hide_index=True,
        column_config={
            "confidence_score": st.column_config.ProgressColumn(
                "Confidence", min_value=0.0, max_value=1.0, format="%.3f"
            )
        },
    )

    st.download_button(
        "Download matrix (Excel)",
        data=build_excel({"Traceability Matrix": links.drop(columns=["id"])}),
        file_name="traceability_matrix.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def render_gaps():
    st.subheader("Gap analysis")
    st.caption(
        "Items that break traceability: requirements with no evidence, and evidence that traces nowhere."
    )

    uncovered = load_uncovered()
    unlinked_test = load_unlinked("TEST")
    unlinked_rmm = load_unlinked("RMM")

    c1, c2, c3 = st.columns(3)
    c1.metric("Uncovered requirements", len(uncovered))
    c2.metric("Unlinked test items", len(unlinked_test))
    c3.metric("Unlinked risk items", len(unlinked_rmm))

    tab1, tab2, tab3 = st.tabs(
        [
            f"Uncovered requirements ({len(uncovered)})",
            f"Unlinked tests ({len(unlinked_test)})",
            f"Unlinked risks ({len(unlinked_rmm)})",
        ]
    )

    with tab1:
        if uncovered.empty:
            st.success("Every requirement has at least one trace link.")
        else:
            st.warning("These requirements have no verifying test or risk control.")
            st.dataframe(uncovered, width="stretch", hide_index=True)

    with tab2:
        if unlinked_test.empty:
            st.success("Every test item traces to a requirement.")
        else:
            st.dataframe(unlinked_test, width="stretch", hide_index=True)

    with tab3:
        if unlinked_rmm.empty:
            st.success("Every risk item traces to a requirement.")
        else:
            st.dataframe(unlinked_rmm, width="stretch", hide_index=True)

    st.download_button(
        "Download gap report (Excel)",
        data=build_excel(
            {
                "Uncovered Requirements": uncovered,
                "Unlinked Tests": unlinked_test,
                "Unlinked Risks": unlinked_rmm,
            }
        ),
        file_name="traceability_gap_report.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def render_review():
    st.subheader("Review queue")
    st.caption("Semantic suggestions below the auto-approve threshold need a human decision.")

    pending = db.get_links(statuses=["NEEDS_REVIEW"])
    if pending.empty:
        st.success("Nothing awaiting review.")
        return

    editable = pending.copy()
    editable.insert(0, "select", False)

    edited = st.data_editor(
        editable,
        width="stretch",
        hide_index=True,
        disabled=[c for c in editable.columns if c != "select"],
        column_config={
            "id": None,
            "confidence_score": st.column_config.ProgressColumn(
                "Confidence", min_value=0.0, max_value=1.0, format="%.3f"
            ),
        },
    )

    selected_ids = edited.loc[edited["select"], "id"].tolist()
    st.caption(f"{len(selected_ids)} link(s) selected.")

    c1, c2 = st.columns(2)
    if c1.button("Approve selected", type="primary", disabled=not selected_ids):
        count = db.update_link_status(selected_ids, "ACTIVE")
        st.cache_data.clear()
        st.success(f"Approved {count} link(s).")
        st.rerun()

    if c2.button("Reject selected", disabled=not selected_ids):
        count = db.update_link_status(selected_ids, "REJECTED")
        st.cache_data.clear()
        st.success(f"Rejected {count} link(s).")
        st.rerun()


def render_explorer():
    st.subheader("Item explorer")
    item_id = st.text_input("Item ID", placeholder="e.g. PR.UniGuide.FR.DataIdentification")

    if not item_id:
        st.info("Enter a requirement, test, or risk ID to inspect its content and links.")
        return

    detail = db.get_item_detail(item_id)
    if detail.empty:
        st.warning(f"No chunk found with item ID '{item_id}'.")
        return

    row = detail.iloc[0]
    c1, c2, c3 = st.columns(3)
    c1.metric("Document", row["parent_doc_id"])
    c2.metric("Doc class", row["doc_type"])
    c3.metric("Item type", row["item_type"])

    with st.expander("Text content", expanded=True):
        st.text(row["text_content"][:4000])
    with st.expander("Metadata"):
        st.json(row["metadata"])

    all_links = db.get_links()
    upstream = all_links[all_links["source_item_id"] == item_id]
    downstream = all_links[all_links["target_item_id"] == item_id]

    st.markdown(f"**Traces to ({len(upstream)})**")
    if upstream.empty:
        st.caption("None.")
    else:
        st.dataframe(upstream.drop(columns=["id"]), width="stretch", hide_index=True)

    st.markdown(f"**Traced from ({len(downstream)})**")
    if downstream.empty:
        st.caption("None.")
    else:
        st.dataframe(downstream.drop(columns=["id"]), width="stretch", hide_index=True)


def main():
    st.title("Automated Traceability Matrix")
    init_state()

    pending_nav = st.session_state.pop("pending_nav", None)
    if pending_nav in PAGES:
        st.session_state.page = pending_nav

    healthy, message = db.check_health()
    if not healthy:
        st.error(f"Cannot reach PostgreSQL: {message}")
        st.info(
            "Start the databases with `docker compose up -d` from the repository root, then reload."
        )
        st.stop()

    if st.session_state.pipeline_running:
        execute_pending_pipeline()
        return

    with st.sidebar:
        st.success("Database connected")
        page = st.radio("View", PAGES, key="page")
        if st.button("Refresh data"):
            st.cache_data.clear()
            st.rerun()
        st.caption("Pipeline: Engine 1 ingest → Engine 2 embed → Engine 3 link")

    render_pipeline_notice()

    if page == "Overview":
        render_overview()
    elif page == "Traceability matrix":
        render_matrix()
    elif page == "Gap analysis":
        render_gaps()
    elif page == "Review queue":
        render_review()
    else:
        render_explorer()


if __name__ == "__main__":
    main()

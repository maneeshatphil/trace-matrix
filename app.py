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

st.set_page_config(page_title="Traceability Matrix", page_icon="🔗", layout="wide")

CACHE_TTL = 60


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


def render_overview():
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
    st.title("🔗 Automated Traceability Matrix")

    healthy, message = db.check_health()
    if not healthy:
        st.error(f"Cannot reach PostgreSQL: {message}")
        st.info(
            "Start the databases with `docker compose up -d` from the repository root, then reload."
        )
        st.stop()

    with st.sidebar:
        st.success("Database connected")
        page = st.radio(
            "View",
            ["Overview", "Traceability matrix", "Gap analysis", "Review queue", "Item explorer"],
        )
        if st.button("Refresh data"):
            st.cache_data.clear()
            st.rerun()
        st.caption("Pipeline: Engine 1 ingest → Engine 2 embed → Engine 3 link")

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

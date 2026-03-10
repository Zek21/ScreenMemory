"""
ScreenMemory — Streamlit Web Search UI
Timeline browser with natural language search, visual history, and activity insights.
Launch: streamlit run ui/app.py
"""
import os
import sys
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

# Add project root
sys.path.insert(0, str(Path(__file__).parent.parent))

import streamlit as st

from core.database import ScreenMemoryDB
from core.lancedb_store import LanceDBStore
from core.embedder import EmbeddingEngine


def load_config():
    config_path = Path(__file__).parent.parent / "config.json"
    if config_path.exists():
        return json.loads(config_path.read_text())
    return {}


def format_timestamp(ts):
    if not ts:
        return "N/A"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def time_ago(ts):
    if not ts:
        return ""
    delta = time.time() - ts
    if delta < 60:
        return f"{int(delta)}s ago"
    elif delta < 3600:
        return f"{int(delta/60)}m ago"
    elif delta < 86400:
        return f"{int(delta/3600)}h ago"
    else:
        return f"{int(delta/86400)}d ago"


@st.cache_resource
def get_db():
    config = load_config()
    db_path = config.get("database", {}).get("path", "data/screen_memory.db")
    if not os.path.exists(db_path):
        return None
    return ScreenMemoryDB(db_path)


@st.cache_resource
def get_lance():
    config = load_config()
    lance_path = config.get("database", {}).get("lance_path", "data/lance_memory")
    if os.path.exists(lance_path):
        return LanceDBStore(lance_path)
    return LanceDBStore(lance_path)


@st.cache_resource
def get_embedder():
    return EmbeddingEngine(prefer_gpu=False)


def main():
    st.set_page_config(
        page_title="ScreenMemory",
        page_icon="🧠",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    st.title("🧠 ScreenMemory")
    st.caption("Your local, private digital timeline")

    # Sidebar
    with st.sidebar:
        st.header("Controls")

        db = get_db()
        lance = get_lance()

        if db:
            stats = db.get_stats()
            st.metric("Total Captures", stats.get("total_captures", 0))
            st.metric("Database Size", f"{stats.get('db_size_mb', 0):.1f} MB")

            oldest = stats.get("oldest_timestamp")
            newest = stats.get("newest_timestamp")
            if oldest and newest:
                st.caption(f"Range: {format_timestamp(oldest)} to {format_timestamp(newest)}")

        if lance and lance.is_available:
            lance_stats = lance.get_stats()
            st.metric("LanceDB Records", lance_stats.get("total_captures", 0))
            st.success("LanceDB: Active")
        else:
            st.warning("LanceDB: Not available")

        st.divider()
        view_mode = st.radio("View", ["Search", "Timeline", "Activity", "Stats"])

    if not db:
        st.error("No database found. Start the daemon first: `python main.py`")
        return

    if view_mode == "Search":
        render_search(db, lance)
    elif view_mode == "Timeline":
        render_timeline(db)
    elif view_mode == "Activity":
        render_activity(db)
    elif view_mode == "Stats":
        render_stats(db, lance)


def render_search(db, lance):
    """Natural language search interface."""
    st.subheader("Search Your History")

    col1, col2 = st.columns([3, 1])
    with col1:
        query = st.text_input("Search", placeholder="What were you working on?")
    with col2:
        search_type = st.selectbox("Type", ["Text", "Hybrid", "App Filter"])

    if query:
        with st.spinner("Searching..."):
            if search_type == "App Filter":
                results = db.get_by_process(query, 30)
            elif search_type == "Hybrid" and lance and lance.is_available:
                embedder = get_embedder()
                if embedder.is_available:
                    query_emb = embedder.embed_text(query)
                    if query_emb is not None:
                        results = lance.search_hybrid(query, query_emb.tolist(), limit=30)
                    else:
                        results = db.search_text(query, 30)
                else:
                    results = db.search_text(query, 30)
            else:
                results = db.search_text(query, 30)

        if results:
            st.success(f"Found {len(results)} results")
            for r in results:
                render_result_card(r)
        else:
            st.info("No results found.")


def render_timeline(db):
    """Visual timeline of recent activity."""
    st.subheader("Timeline")

    col1, col2 = st.columns(2)
    with col1:
        limit = st.slider("Show entries", 10, 100, 30)
    with col2:
        hours_back = st.slider("Hours back", 1, 168, 24)

    start_ts = time.time() - (hours_back * 3600)
    results = db.get_by_timerange(start_ts, time.time())

    if not results:
        results = db.get_recent(limit)

    if results:
        st.info(f"Showing {len(results)} entries")

        # Group by hour
        groups = {}
        for r in results:
            ts = r.get("timestamp", 0)
            hour_key = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:00")
            groups.setdefault(hour_key, []).append(r)

        for hour, entries in sorted(groups.items(), reverse=True):
            with st.expander(f"**{hour}** ({len(entries)} captures)", expanded=False):
                for r in entries:
                    render_result_card(r)
    else:
        st.info("No captures yet. Start the daemon: `python main.py`")


def render_activity(db):
    """Activity breakdown by application."""
    st.subheader("Activity Breakdown")

    results = db.get_recent(500)
    if not results:
        st.info("No data yet.")
        return

    # Group by process
    app_counts = {}
    for r in results:
        process = r.get("active_process", "unknown")
        app_counts[process] = app_counts.get(process, 0) + 1

    sorted_apps = sorted(app_counts.items(), key=lambda x: x[1], reverse=True)

    # Simple bar display
    for app, count in sorted_apps[:15]:
        col1, col2 = st.columns([2, 5])
        with col1:
            st.write(f"**{app}**")
        with col2:
            st.progress(count / max(c for _, c in sorted_apps), text=f"{count} captures")


def render_stats(db, lance):
    """Detailed statistics."""
    st.subheader("System Statistics")

    stats = db.get_stats()

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Total Captures", stats.get("total_captures", 0))
        st.metric("FTS Available", "Yes" if stats.get("fts_available") else "No")
    with col2:
        st.metric("DB Size", f"{stats.get('db_size_mb', 0):.2f} MB")
        st.metric("Vec Available", "Yes" if stats.get("vec_available") else "No")
    with col3:
        if lance and lance.is_available:
            lance_stats = lance.get_stats()
            st.metric("LanceDB Records", lance_stats.get("total_captures", 0))
            st.metric("LanceDB Size", f"{lance_stats.get('db_size_mb', 0):.2f} MB")

    st.json(stats)


def render_result_card(r):
    """Render a single search result as a card."""
    ts = r.get("timestamp", 0)
    process = r.get("active_process", "?")
    title = r.get("active_window_title", "")
    analysis = r.get("analysis_text", "")
    ocr = r.get("ocr_text", "")

    with st.container():
        cols = st.columns([1, 4])
        with cols[0]:
            st.caption(format_timestamp(ts))
            st.caption(time_ago(ts))
        with cols[1]:
            st.markdown(f"**{process}** - {title[:80]}")
            if analysis:
                st.write(analysis[:200])
            if ocr and ocr != analysis:
                with st.expander("OCR Text"):
                    st.text(ocr[:500])
        st.divider()


if __name__ == "__main__":
    main()

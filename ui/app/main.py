"""
ui/app/main.py

Entry point for the Incident Copilot Streamlit application.

Responsibilities:
  - Fetches COMPANY_NAME and public config from the FastAPI /config endpoint (cached 60s).
  - Renders the sidebar with active & resolved incidents, health status, and metadata.
  - Provides an incident creation form to register new incidents via the API.
  - Displays selected incident details, metadata, and conversation history.
  - Provides interactive triage chat with source citations, cache indicators, and guardrails.
  - Supports resolving active incidents with optional AI-synthesized post-mortem reports.
  - Renders post-mortems with tabbed preview/source, download, and knowledge base publishing.
  - Maintains clean session state for tracking selected incidents.
  - Provides an Admin tab for knowledge base management (upload, inventory, vector search).

All backend communication goes through the FastAPI service; this file contains
NO business logic, RAG calls, or direct database/redis access.
"""

from datetime import datetime
import os
from typing import Any, Dict, List, Optional

import requests
import streamlit as st

# ── Constants ────────────────────────────────────────────────────────────────

# API_URL is set to http://api:8000 inside Docker (via docker-compose environment:).
# Falls back to localhost for running the UI outside Docker during development.
API_URL: str = os.getenv("API_URL", "http://localhost:8000")

# Severity emoji mapping
SEVERITY_EMOJI_MAP: Dict[str, str] = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "🟢",
}

# Status emoji mapping
STATUS_EMOJI_MAP: Dict[str, str] = {
    "active": "🟢",
    "resolved": "⚪",
}

# Document type badge mapping
DOC_TYPE_BADGES: Dict[str, str] = {
    "architecture": "🏷️ architecture",
    "runbook": "📘 runbook",
    "postmortem": "📝 postmortem",
}


def get_severity_emoji(severity: Optional[str]) -> str:
    """Returns the visual indicator emoji for a given incident severity."""
    if not severity:
        return "🟡"
    return SEVERITY_EMOJI_MAP.get(severity.lower(), "🟡")


def get_status_emoji(status: Optional[str]) -> str:
    """Returns the visual indicator emoji for a given incident status."""
    if not status:
        return "⚪"
    return STATUS_EMOJI_MAP.get(status.lower(), "⚪")


def format_timestamp(timestamp_str: Optional[str]) -> str:
    """
    Formats an ISO 8601 timestamp string into a human-readable UTC representation.

    Falls back to the raw string if parsing fails.
    """
    if not timestamp_str:
        return "—"
    try:
        dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return str(timestamp_str)


# ── Backend helpers ───────────────────────────────────────────────────────────


def fetch_config() -> Dict[str, Any]:
    """
    Fetches public configuration from the FastAPI /config endpoint.

    Raises an exception on any network or HTTP error so that the caller can
    decide whether to use a fallback value. This keeps the cached function
    (get_cached_config) from storing a failed/empty response.
    """
    response = requests.get(f"{API_URL}/config", timeout=10)
    response.raise_for_status()
    return response.json()


def check_api_health() -> bool:
    """
    Pings the FastAPI /health endpoint.

    Returns True if the API responds with HTTP 200, False otherwise.
    Used to render the connection status badge in the sidebar.
    """
    try:
        response = requests.get(f"{API_URL}/health", timeout=10)
        return response.status_code == 200
    except Exception:
        return False


def fetch_incidents() -> Optional[List[Dict[str, Any]]]:
    """
    Fetches the list of recent incidents from GET /incidents?limit=200.

    DO NOT cache this function: it must be fetched freshly on every Streamlit
    rerun so that updates are immediate.
    Returns a list of incident dictionaries, or None if the request failed.
    """
    try:
        response = requests.get(f"{API_URL}/incidents", params={"limit": 200}, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and "incidents" in data:
                return data["incidents"]
            if isinstance(data, dict) and "items" in data:
                return data["items"]
            return []
        return None
    except Exception:
        return None


def fetch_incident(incident_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetches details for a single incident by its ID from GET /incidents/{incident_id}.

    Returns the incident dictionary if successful, or None if not found or on error.
    """
    try:
        response = requests.get(f"{API_URL}/incidents/{incident_id}", timeout=10)
        if response.status_code == 200:
            return response.json()
        return None
    except Exception:
        return None


def format_429_warning(response: requests.Response) -> str:
    """Formats user-facing warning message for HTTP 429 rate limit or daily cap errors."""
    retry_after = response.headers.get("Retry-After", "")
    is_daily = False
    try:
        resp_json = response.json()
        if isinstance(resp_json, dict) and "detail" in resp_json:
            if "daily" in str(resp_json["detail"]).lower():
                is_daily = True
    except Exception:
        if "daily" in response.text.lower():
            is_daily = True

    if is_daily:
        return "The daily limit was reached."
    if retry_after:
        return f"Too many requests right now. Try again in {retry_after} seconds."
    return "Too many requests right now. Please try again shortly."


def ask_incident_question(incident_id: str, question: str) -> None:
    """
    Submits a triage question to the incident copilot API and handles the response.

    Posts to POST /incidents/{incident_id}/ask with a 90-second timeout.
    On success (HTTP 200), triggers a Streamlit rerun to display the copilot response.
    On error (404, 409, 503, or network errors), extracts the error detail from the
    response payload if available and displays a friendly error message without stack traces.

    Args:
        incident_id: UUID string of the target incident ticket.
        question: Non-empty question text submitted by the user.
    """
    with st.spinner("Searching runbooks & consulting copilot..."):
        try:
            response = requests.post(
                f"{API_URL}/incidents/{incident_id}/ask",
                json={"question": question},
                timeout=90,
            )
            if response.status_code == 200:
                st.rerun()
            elif response.status_code == 429:
                st.warning(format_429_warning(response))
            else:
                detail: Optional[str] = None
                try:
                    resp_json = response.json()
                    if isinstance(resp_json, dict) and "detail" in resp_json:
                        raw_detail = resp_json["detail"]
                        if isinstance(raw_detail, list):
                            detail = "; ".join(
                                item.get("msg", str(item)) if isinstance(item, dict) else str(item)
                                for item in raw_detail
                            )
                        else:
                            detail = str(raw_detail)
                except Exception:
                    detail = None

                if not detail:
                    if response.status_code == 404:
                        detail = "Incident not found."
                    elif response.status_code == 409:
                        detail = "Incident is resolved and cannot accept new triage questions."
                    elif response.status_code == 503:
                        detail = "Copilot service is currently unavailable. Please try again later."
                    else:
                        detail = f"Server returned error code {response.status_code}."

                st.error(f"Error: {detail}")
        except requests.exceptions.Timeout:
            st.error("Error: The copilot request timed out after 90 seconds. Please try again.")
        except requests.exceptions.ConnectionError:
            st.error("Error: Could not connect to the backend server. Please verify the service is running.")
        except Exception as exc:
            st.error(f"Error: Network or request error occurred: {exc}")


def render_chat_message(message: Dict[str, Any]) -> None:
    """
    Renders an individual chat message in the incident conversation thread.

    For assistant messages, displays metadata badges and guardrail alerts:
      - Cache status badge (Redis cache hit indicator or latency/token usage caption).
      - Collapsible Sources expander with document references and similarity scores.
      - Guardrail warning when unverified commands are redacted.
      - High-risk caution alerts for destructive commands.

    Args:
        message: Dictionary containing 'role', 'content', and optional 'meta' fields.
    """
    role = message.get("role", "user")
    content = message.get("content", "")

    with st.chat_message(role):
        st.markdown(content)

        meta = message.get("meta")
        if role == "assistant" and meta is not None and isinstance(meta, dict):
            # Cache Status Badge:
            if meta.get("cache_status") == "hit":
                st.success("🟢 Served from Redis cache (0 LLM tokens)")
            else:
                resp_ms = meta.get("response_ms", 0)
                try:
                    resp_ms_val = float(resp_ms) if resp_ms is not None else 0.0
                except (ValueError, TypeError):
                    resp_ms_val = 0.0

                tokens_in = meta.get("tokens_in", 0)
                if tokens_in is None:
                    tokens_in = 0

                tokens_out = meta.get("tokens_out", 0)
                if tokens_out is None:
                    tokens_out = 0

                model_name = meta.get("model", "LLM")
                if not model_name:
                    model_name = "LLM"

                st.caption(f"⏱️ {resp_ms_val:.0f}ms • {tokens_in} in / {tokens_out} out • {model_name}")

            # Sources Expander:
            sources = meta.get("sources", [])
            if sources and isinstance(sources, list):
                with st.expander(f"📚 Sources ({len(sources)})", expanded=False):
                    for s in sources:
                        if isinstance(s, dict):
                            n_val = s.get("n")
                            fname = s.get("filename")
                            dtype = s.get("doc_type")
                            section = s.get("section") or "General"
                            raw_score = s.get("score", 0)
                            try:
                                score_val = float(raw_score) if raw_score is not None else 0.0
                            except (ValueError, TypeError):
                                score_val = 0.0
                            st.markdown(
                                f"**[{n_val}] {fname}** ({dtype}) › {section} `[{score_val:.3f}]`"
                            )

            # Guardrail Warning:
            if meta.get("guardrail") == "redacted":
                unverified = meta.get("unverified_commands", [])
                count = len(unverified) if unverified else "one or more"
                st.warning(
                    f"⚠️ {count} unverified command(s) removed because they did not appear in any retrieved runbook."
                )

            # Destructive Command Caution:
            destructives = meta.get("destructive_commands", [])
            if destructives and isinstance(destructives, list):
                for d in destructives:
                    st.error(f"⚠️ CAUTION: Verified command includes high-risk operation: `{d}`")


# ── Cached Configuration ──────────────────────────────────────────────────────


@st.cache_data(ttl=60, show_spinner=False)
def get_cached_config() -> Dict[str, Any]:
    """
    Returns the /config payload, cached for 60 seconds.

    Only caches successful responses — if fetch_config raises, the exception
    propagates and Streamlit does NOT cache the error state. This prevents a
    transient API outage from being stored and served for 60 seconds.
    """
    return fetch_config()


# Attempt to load config; use a safe fallback if the API is not yet reachable.
try:
    config = get_cached_config()
except Exception:
    config = {
        "company_name": "Incident Copilot",
        "llm_provider": "unknown",
        "embedding_model": "unknown",
    }

company_name: str = config.get("company_name", "Incident Copilot")

# ── Page setup ────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title=f"{company_name} — Incident Copilot",
    page_icon="🚨",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Session State Initialisation ──────────────────────────────────────────────

if "selected_incident_id" not in st.session_state:
    st.session_state["selected_incident_id"] = None

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("🚨 Incident Copilot")
    st.caption(f"Powered by **{company_name}**")
    st.divider()

    # API health status badge
    api_ok: bool = check_api_health()
    if api_ok:
        st.success("✅ API connected", icon=None)
    else:
        st.error("❌ API unreachable", icon=None)

    st.subheader("Active & Resolved Incidents")

    # "+ New incident" button resets selection to display creation form
    if st.button("➕ New incident", use_container_width=True, type="secondary"):
        st.session_state["selected_incident_id"] = None
        st.rerun()

    # Freshly fetch incident list on every rerun
    incidents_list = fetch_incidents()

    if incidents_list is None:
        st.error("Unable to load incidents from server.")
    else:
        active_incidents = [inc for inc in incidents_list if inc.get("status") == "active"]
        resolved_incidents = [inc for inc in incidents_list if inc.get("status") == "resolved"]

        # Active Incidents section
        st.markdown("#### Active Incidents")
        if active_incidents:
            for inc in active_incidents:
                inc_id_str = str(inc.get("id"))
                is_selected = inc_id_str == str(st.session_state.get("selected_incident_id"))
                emoji = get_severity_emoji(inc.get("severity"))
                raw_title = inc.get("title", "Untitled")
                display_title = raw_title if len(raw_title) <= 30 else f"{raw_title[:27]}..."
                button_label = f"▶ {emoji} {display_title}" if is_selected else f"{emoji} {display_title}"

                if st.button(
                    button_label,
                    key=f"active_inc_{inc_id_str}",
                    type="primary" if is_selected else "secondary",
                    use_container_width=True,
                ):
                    st.session_state["selected_incident_id"] = inc_id_str
                    st.rerun()
        else:
            st.caption("No active incidents.")

        # Resolved Incidents section
        st.markdown("#### Resolved Incidents")
        if resolved_incidents:
            for inc in resolved_incidents:
                inc_id_str = str(inc.get("id"))
                is_selected = inc_id_str == str(st.session_state.get("selected_incident_id"))
                emoji = get_severity_emoji(inc.get("severity"))
                raw_title = inc.get("title", "Untitled")
                display_title = raw_title if len(raw_title) <= 30 else f"{raw_title[:27]}..."
                button_label = f"▶ {emoji} {display_title}" if is_selected else f"{emoji} {display_title}"

                if st.button(
                    button_label,
                    key=f"resolved_inc_{inc_id_str}",
                    type="primary" if is_selected else "secondary",
                    use_container_width=True,
                ):
                    st.session_state["selected_incident_id"] = inc_id_str
                    st.rerun()
        else:
            st.caption("No resolved incidents.")

    st.divider()
    st.caption(f"LLM: `{config.get('llm_provider', '—')}`")
    st.caption(f"Embeddings: `{config.get('embedding_model', '—')}`")

# ── Main Content ──────────────────────────────────────────────────────────────

st.header(f"🚨 {company_name} — Incident Copilot")

tab_chat, tab_admin = st.tabs(["💬 Incident Chat", "⚙️ Admin"])

# ── Tab 1: Incident Chat ──────────────────────────────────────────────────────

with tab_chat:
    chat_flash = st.session_state.pop("chat_flash_message", None)
    if chat_flash:
        st.success(chat_flash)

    selected_id = st.session_state.get("selected_incident_id")

    if selected_id is None:
        # Create Incident Form view
        st.markdown("### 🚨 Create New Incident")

        with st.form("create_incident_form", clear_on_submit=False):
            title_input = st.text_input(
                "Incident Title *",
                placeholder="e.g., Database connection pool exhausted",
            )
            severity_input = st.selectbox(
                "Severity",
                options=["medium", "low", "high", "critical"],
                index=0,
            )
            description_input = st.text_area(
                "Description",
                placeholder="Describe symptoms, affected services, alert details...",
            )
            submitted = st.form_submit_button("Create Incident", type="primary")

            if submitted:
                clean_title = title_input.strip()
                if not (3 <= len(clean_title) <= 200):
                    st.warning("Title must be between 3 and 200 characters.")
                else:
                    payload = {
                        "title": clean_title,
                        "severity": severity_input,
                        "description": description_input.strip() if description_input.strip() else None,
                    }
                    try:
                        response = requests.post(f"{API_URL}/incidents", json=payload, timeout=10)
                        if response.status_code == 201:
                            data = response.json()
                            new_id = data.get("id")
                            st.session_state["selected_incident_id"] = str(new_id)
                            st.success("Incident created successfully!")
                            st.rerun()
                        else:
                            st.error(f"Failed to create incident: {response.text}")
                    except Exception:
                        st.error("Failed to connect to the server to create the incident. Please try again.")

    else:
        # Selected Incident Detail & History View
        incident = fetch_incident(selected_id)

        if not incident:
            st.error("Incident not found or could not be loaded.")
            if st.button("Back to New Incident", type="primary"):
                st.session_state["selected_incident_id"] = None
                st.rerun()
        else:
            incident_title = incident.get("title", "Untitled Incident")
            raw_severity = str(incident.get("severity", "medium")).lower()
            raw_status = str(incident.get("status", "active")).lower()
            formatted_created = format_timestamp(incident.get("created_at"))

            sev_emoji = get_severity_emoji(raw_severity)
            stat_emoji = get_status_emoji(raw_status)

            # Header with title and metadata
            st.subheader(f"{sev_emoji} {incident_title}")

            col1, col2, col3 = st.columns(3)
            with col1:
                st.markdown(f"**Severity:** {sev_emoji} `{raw_severity.upper()}`")
            with col2:
                st.markdown(f"**Status:** {stat_emoji} `{raw_status.upper()}`")
            with col3:
                st.markdown(f"**Created:** `{formatted_created}`")

            incident_desc = incident.get("description")
            if incident_desc:
                with st.expander("Incident Description", expanded=True):
                    st.markdown(incident_desc)

            st.divider()

            # Conversation history
            messages = incident.get("messages", [])
            if messages:
                for message in messages:
                    render_chat_message(message)
            else:
                st.info("No messages in this incident yet.")

            # Incident status-dependent actions
            if raw_status == "active":
                user_question = st.chat_input("Ask a triage question...")
                if user_question:
                    clean_question = user_question.strip()
                    if clean_question:
                        ask_incident_question(selected_id, clean_question)

                with st.expander("Resolve incident", expanded=False):
                    resolution_summary = st.text_area(
                        "Resolution summary (recommended)",
                        max_chars=2000,
                        help="Brief explanation of how the incident was mitigated or fixed.",
                    )
                    generate_pm = st.checkbox(
                        "Generate post-mortem",
                        value=True,
                        help="Automatically synthesize a post-mortem report using incident transcript.",
                    )
                    confirmed = st.checkbox(
                        "I confirm this incident is resolved",
                        value=False,
                    )
                    resolve_btn = st.button(
                        "Resolve Incident & Generate Post-Mortem",
                        type="primary",
                        disabled=not confirmed,
                        use_container_width=True,
                    )

                    if resolve_btn:
                        if confirmed:
                            with st.spinner("Generating post-mortem..."):
                                payload = {
                                    "resolution_summary": resolution_summary.strip() if resolution_summary else None,
                                    "generate_postmortem": generate_pm,
                                }
                                try:
                                    resp = requests.post(
                                        f"{API_URL}/incidents/{selected_id}/resolve",
                                        json=payload,
                                        timeout=200,
                                    )
                                    if resp.status_code == 200:
                                        st.session_state["chat_flash_message"] = "Incident resolved successfully."
                                        st.rerun()
                                    elif resp.status_code == 429:
                                        st.warning(format_429_warning(resp))
                                    elif resp.status_code == 404:
                                        st.error("Incident not found.")
                                    elif resp.status_code == 409:
                                        try:
                                            st.error(resp.json().get("detail", "Incident is already resolved or resolution is currently in progress."))
                                        except Exception:
                                            st.error("Incident is already resolved or resolution is currently in progress.")
                                    elif resp.status_code == 422:
                                        st.error("Nothing to summarize. Please provide a resolution summary or message in the thread.")
                                    elif resp.status_code == 503:
                                        st.error("The language model is unavailable or rate limited. Try again shortly.")
                                    else:
                                        st.error("Failed to resolve incident. Please try again.")
                                except requests.exceptions.Timeout:
                                    st.error("This is taking longer than expected. Refresh the incident before trying again.")
                                except Exception:
                                    st.error("Failed to resolve incident. Please try again.")

            elif raw_status == "resolved":
                st.info("ℹ️ This incident is resolved.")
                postmortem_md = incident.get("postmortem_md")
                if postmortem_md:
                    tab_rendered, tab_source = st.tabs(["📄 Rendered", "📝 Markdown source"])
                    with tab_rendered:
                        st.markdown(postmortem_md)
                    with tab_source:
                        st.code(postmortem_md, language="markdown")

                    title_slug = incident.get("title", "incident").lower().replace(" ", "-")
                    st.download_button(
                        label="⬇️ Download post-mortem (.md)",
                        data=postmortem_md,
                        file_name=f"postmortem-{title_slug}.md",
                        mime="text/markdown",
                        use_container_width=True,
                    )

                    if st.button("📚 Add to knowledge base", type="secondary", use_container_width=True):
                        with st.spinner("Publishing post-mortem to knowledge base..."):
                            try:
                                res = requests.post(f"{API_URL}/incidents/{selected_id}/postmortem/publish", timeout=60)
                                if res.status_code == 201:
                                    filename = res.json().get("filename")
                                    chunk_count = res.json().get("chunk_count")
                                    st.success(f"Published {filename} ({chunk_count} chunks) to knowledge base.")
                                else:
                                    try:
                                        st.error(res.json().get("detail", "Failed to publish post-mortem to knowledge base."))
                                    except Exception:
                                        st.error("Failed to publish post-mortem to knowledge base.")
                            except Exception:
                                st.error("Failed to publish post-mortem to knowledge base.")

                    st.caption("Publishing adds the post-mortem to the team's knowledge base, making it searchable across all incidents and refreshing cached answers.")
                else:
                    st.info("No post-mortem was generated.")
            else:
                st.info(f"ℹ️ Incident status is '{raw_status}'. Create a new incident to ask questions.")

# ── Tab 2: Admin ──────────────────────────────────────────────────────────────

with tab_admin:
    # ── 1. Admin Flash Message ────────────────────────────────────────────────
    flash_msg = st.session_state.pop("admin_flash_message", None)
    if flash_msg:
        msg_type = flash_msg.get("type", "info")
        msg_text = flash_msg.get("text", "")
        if msg_type == "success":
            st.success(msg_text)
        elif msg_type == "error":
            st.error(msg_text)
        elif msg_type == "warning":
            st.warning(msg_text)
        else:
            st.info(msg_text)

    # ── 2. Upload & Index Document ────────────────────────────────────────────
    st.markdown("### 📄 Upload & Index Document")
    file = st.file_uploader(
        "Select document (.md, .markdown, .txt, .pdf)",
        type=["md", "markdown", "txt", "pdf"],
    )
    doc_type = st.selectbox(
        "Document Type",
        options=["architecture", "runbook", "postmortem"],
    )
    upload_btn = st.button("Upload & Index", type="primary")

    if upload_btn:
        if file is None:
            st.warning("Please select a file to upload.")
        else:
            with st.spinner(f"Chunking, embedding, and indexing {file.name}..."):
                try:
                    files = {
                        "file": (
                            file.name,
                            file.getvalue(),
                            file.type or "application/octet-stream",
                        )
                    }
                    form_data = {"doc_type": doc_type}
                    response = requests.post(
                        f"{API_URL}/knowledge/documents",
                        files=files,
                        data=form_data,
                        timeout=120,
                    )
                    if response.status_code == 201:
                        data = response.json()
                        replaced_msg = (
                            " (replaced older version)"
                            if data.get("replaced")
                            else ""
                        )
                        st.session_state["admin_flash_message"] = {
                            "type": "success",
                            "text": (
                                f"Successfully indexed '{data.get('filename')}' "
                                f"({data.get('chunk_count')} chunks, "
                                f"{data.get('char_count')} chars){replaced_msg}."
                            ),
                        }
                        st.rerun()
                    elif response.status_code == 429:
                        st.session_state["admin_flash_message"] = {
                            "type": "warning",
                            "text": format_429_warning(response),
                        }
                        st.rerun()
                    else:
                        try:
                            err_detail = response.json().get("detail", response.text)
                        except Exception:
                            err_detail = response.text
                        st.session_state["admin_flash_message"] = {
                            "type": "error",
                            "text": f"Upload failed: {err_detail}",
                        }
                        st.rerun()
                except Exception as exc:
                    st.session_state["admin_flash_message"] = {
                        "type": "error",
                        "text": f"Upload failed due to network error: {exc}",
                    }
                    st.rerun()

    # ── 3. Indexed Documents Inventory ────────────────────────────────────────
    st.divider()
    st.markdown("### 📚 Knowledge Base Documents")

    docs: Optional[List[Dict[str, Any]]] = None
    try:
        docs_resp = requests.get(f"{API_URL}/knowledge/documents", timeout=10)
        if docs_resp.status_code == 200:
            resp_data = docs_resp.json()
            if isinstance(resp_data, list):
                docs = resp_data
            elif isinstance(resp_data, dict) and "documents" in resp_data:
                docs = resp_data["documents"]
            elif isinstance(resp_data, dict) and "items" in resp_data:
                docs = resp_data["items"]
            else:
                docs = []
        else:
            st.error(f"Failed to fetch documents: {docs_resp.text}")
    except Exception as exc:
        st.error(f"Failed to connect to knowledge service: {exc}")

    if docs is not None:
        if not docs:
            st.info("No documents indexed yet. Upload architecture overviews, runbooks, or post-mortems above.")
        else:
            for doc in docs:
                doc_id = doc.get("id")
                filename = doc.get("filename", "Untitled")
                raw_dtype = str(doc.get("doc_type", "")).lower()
                badge = DOC_TYPE_BADGES.get(raw_dtype, f"📄 {raw_dtype}")
                chunk_cnt = doc.get("chunk_count", 0)
                char_cnt = doc.get("char_count", 0)
                updated_at_str = format_timestamp(doc.get("updated_at"))

                with st.container():
                    col_info, col_del = st.columns([5, 1])
                    with col_info:
                        st.markdown(f"**{badge}** &nbsp; **{filename}**")
                        st.caption(f"{chunk_cnt} chunks • {char_cnt} chars • Updated: {updated_at_str}")
                    with col_del:
                        if st.button("🗑️ Delete", key=f"del_doc_{doc_id}", type="secondary"):
                            try:
                                del_response = requests.delete(
                                    f"{API_URL}/knowledge/documents/{doc_id}",
                                    timeout=10,
                                )
                                if del_response.status_code == 204:
                                    st.session_state["admin_flash_message"] = {
                                        "type": "success",
                                        "text": f"Deleted document '{filename}'.",
                                    }
                                    st.rerun()
                                else:
                                    st.session_state["admin_flash_message"] = {
                                        "type": "error",
                                        "text": f"Failed to delete document: {del_response.text}",
                                    }
                                    st.rerun()
                            except Exception as del_err:
                                st.session_state["admin_flash_message"] = {
                                    "type": "error",
                                    "text": f"Failed to delete document due to network error: {del_err}",
                                }
                                st.rerun()

    # ── 4. Test Vector Search ─────────────────────────────────────────────────
    st.divider()
    st.markdown("### 🔍 Test Vector Search")
    st.caption("Query the FAISS vector index directly to inspect chunk retrieval and similarity scores.")

    search_query = st.text_input(
        "Search query",
        placeholder="e.g. database connection pool exhaustion",
    )
    col_k, col_filter = st.columns(2)
    with col_k:
        k_val = st.slider("Max results (k)", min_value=1, max_value=10, value=5)
    with col_filter:
        filter_type = st.selectbox(
            "Filter doc type",
            options=["All", "architecture", "runbook", "postmortem"],
        )

    search_btn = st.button("Search Knowledge Base")
    if search_btn:
        if not search_query.strip():
            st.warning("Please enter a query.")
        else:
            with st.spinner("Searching vector index..."):
                payload: Dict[str, Any] = {
                    "query": search_query.strip(),
                    "k": k_val,
                }
                if filter_type != "All":
                    payload["doc_type"] = filter_type

                try:
                    search_resp = requests.post(
                        f"{API_URL}/knowledge/search",
                        json=payload,
                        timeout=10,
                    )
                    if search_resp.status_code == 200:
                        res_data = search_resp.json()
                        hits = res_data.get("hits", [])
                        search_ms = float(res_data.get("search_ms", 0.0))
                        st.caption(f"Found {len(hits)} matching chunks in {search_ms:.1f} ms")
                        if not hits:
                            st.info("No matching chunks found.")
                        else:
                            for i, hit in enumerate(hits, 1):
                                score = float(hit.get("score", 0.0))
                                fname = hit.get("filename", "unknown")
                                section = hit.get("section") or "General"
                                expander_label = f"#{i} [{score:.3f}] {fname} › {section}"
                                with st.expander(expander_label):
                                    st.markdown(
                                        f"**Doc Type:** `{hit.get('doc_type')}` &nbsp;|&nbsp; "
                                        f"**Chunk ID:** `{hit.get('chunk_id')}`"
                                    )
                                    chunk_text = hit.get("text", "")
                                    st.markdown(chunk_text)
                    else:
                        st.error(f"Search failed ({search_resp.status_code}): {search_resp.text}")
                except Exception as search_err:
                    st.error(f"Failed to connect to search service: {search_err}")

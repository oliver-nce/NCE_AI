import json
import re

import frappe
import requests
from frappe import _
from frappe.utils.password import get_decrypted_password

SYSTEM_PROMPT = """You are a helpful assistant for people working on a live site that runs the Frappe Framework, installed and run through bench (Python app server, Redis, background workers, and MariaDB or PostgreSQL).

Environment you must assume:
- Code and apps live under a bench directory; this site is one entry under sites/. Custom apps are under apps/.
- The data model is defined by DocTypes. Each standard DocType is stored in a SQL table whose name is given by the framework (typically the `tab` prefix plus the DocType name, e.g. `tabCustomer` or `tabSales Order` — exact spelling including spaces matters in MariaDB).
- Child tables (Table fieldtype) are separate DocTypes with their own `tab...` tables and are linked by `parent`, `parenttype`, `parentfield`.
- Do not invent field or table names. When you need schema details, use the provided tools to inspect DocTypes and SQL columns. If a tool reports a permission error, tell the user what access is needed.

Natural-language questions that need data from several DocTypes / tables:
- Use attached context documents (if any) plus get_frappe_doctype_schema and describe_frappe_doctype_sql_table to learn exact `tab...` table names, Link fields, dates, and child-table links (`parent`, `parenttype`, `parentfield`).
- Design one read-only SQL query (JOINs, WHERE, subqueries) that answers the question. Prefer explicit joins on documented keys rather than guessing.
- Execute it only via run_readonly_select_query (single SELECT or WITH … SELECT). That tool is restricted to privileged roles; if it returns a permission error, explain what role is needed.
- If dates or business rules are ambiguous (e.g. "age 14 to 18" vs calendar years), ask a brief clarifying question before running SQL.

Persisting notes for future Agent Chat turns:
- You may create or update **AI Context Document** records via the provided tools (same DocType users attach in the chat picker). Use **context** for reference text and **prompt_injections** for instructions that should apply only at the start of a new chat. Respect the user's permission: they need create/write on that DocType.

When the user's task or request is ambiguous, ask short, specific clarifying questions before doing heavy work or giving a final answer. If the request is clear enough, answer directly."""

CONNECT_TIMEOUT = 15
READ_TIMEOUT = 120
MAX_TOOL_ROUNDS = 12
MAX_SQL_LIMIT = 500
DEFAULT_SQL_LIMIT = 200
MAX_CONTEXT_CHARS = 100_000
MAX_PROMPT_INJECTION_CHARS = 32_000
MAX_TOOL_OUTPUT_CHARS = 120_000
MAX_CONTEXT_DOC_FIELD_CHARS = 150_000

_SQL_FORBIDDEN = re.compile(
	r"\b(insert|update|delete|truncate|drop|alter|create|replace|grant|revoke)\b"
	r"|\binto\s+(outfile|dumpfile)\b|\bload\s+data\b"
	r"|\bfor\s+update\b|\block\s+in\s+share\s+mode\b",
	re.I | re.DOTALL,
)


def _prepare_readonly_select(sql: str) -> tuple[str | None, str]:
	"""Return (executable_sql, error_message). error_message empty on success."""
	core = (sql or "").strip().rstrip(";").strip()
	if not core:
		return None, _("Empty SQL.")
	if ";" in core:
		return None, _("Only one statement is allowed (no semicolons inside the query).")
	low = core.lstrip().lower()
	if not (low.startswith("select") or low.startswith("with")):
		return None, _("Only SELECT or WITH … SELECT is allowed.")
	if _SQL_FORBIDDEN.search(core):
		return None, _("Query contains a forbidden keyword or pattern.")
	lim_pat = re.compile(r"(?is)\blimit\s+(\d+)(\s+offset\s+(\d+))?\s*$")
	m = lim_pat.search(core)
	if m:
		n = min(int(m.group(1)), MAX_SQL_LIMIT)
		tail = f"LIMIT {n}{m.group(2) or ''}"
		prepared = core[: m.start()] + tail
	else:
		prepared = f"{core} LIMIT {min(DEFAULT_SQL_LIMIT, MAX_SQL_LIMIT)}"
	return prepared, ""


def _truncate_context_doc_field(value: str | None) -> str:
	if not value:
		return ""
	if len(value) > MAX_CONTEXT_DOC_FIELD_CHARS:
		return value[:MAX_CONTEXT_DOC_FIELD_CHARS] + "\n\n[Truncated by server limit.]"
	return value


def _arg_bool(arguments: dict, key: str, default: bool = False) -> bool:
	if key not in arguments:
		return default
	v = arguments[key]
	if isinstance(v, bool):
		return v
	if isinstance(v, (int, float)):
		return v != 0
	return str(v).strip().lower() in ("1", "true", "yes", "on")


def _tool_create_ai_context_document(arguments: dict) -> str:
	frappe.has_permission("AI Context Document", "create", throw=True)
	title = (arguments.get("title") or "").strip()
	if not title:
		return json.dumps({"error": "title is required"})
	context = _truncate_context_doc_field(arguments.get("context") or "")
	prompt_injections = _truncate_context_doc_field(arguments.get("prompt_injections") or "")
	doc = frappe.get_doc(
		{
			"doctype": "AI Context Document",
			"title": title,
			"context": context,
			"prompt_injections": prompt_injections,
		}
	)
	doc.insert()
	frappe.logger("nce_ai").info(
		"create_ai_context_document | user={0} | name={1}".format(frappe.session.user, doc.name)
	)
	return json.dumps(
		{
			"name": doc.name,
			"title": doc.title,
			"message": _("Created AI Context Document. User can select it in Agent Chat reference documents."),
		},
		default=str,
	)


def _tool_update_ai_context_document(arguments: dict) -> str:
	doc_name = (arguments.get("document_name") or "").strip()
	if not doc_name:
		return json.dumps({"error": "document_name is required"})
	doc = frappe.get_doc("AI Context Document", doc_name)
	doc.check_permission("write")
	updated = []
	if arguments.get("title") is not None:
		t = str(arguments["title"]).strip()
		if t:
			doc.title = t
			updated.append("title")
	if "context" in arguments and arguments["context"] is not None:
		newc = _truncate_context_doc_field(str(arguments["context"]))
		if _arg_bool(arguments, "append_context"):
			doc.context = (doc.context or "") + ("\n\n---\n\n" if (doc.context or "").strip() else "") + newc
		else:
			doc.context = newc
		updated.append("context")
	if "prompt_injections" in arguments and arguments["prompt_injections"] is not None:
		newp = _truncate_context_doc_field(str(arguments["prompt_injections"]))
		if _arg_bool(arguments, "append_prompt_injections"):
			doc.prompt_injections = (doc.prompt_injections or "") + (
				"\n\n---\n\n" if (doc.prompt_injections or "").strip() else ""
			) + newp
		else:
			doc.prompt_injections = newp
		updated.append("prompt_injections")
	if not updated:
		return json.dumps({"error": "No fields to update. Pass title, context, and/or prompt_injections."})
	doc.save()
	frappe.logger("nce_ai").info(
		"update_ai_context_document | user={0} | name={1} | fields={2}".format(
			frappe.session.user, doc.name, ",".join(updated)
		)
	)
	return json.dumps(
		{
			"name": doc.name,
			"title": doc.title,
			"updated_fields": updated,
			"message": _("Updated AI Context Document."),
		},
		default=str,
	)


def _tool_run_readonly_select_query(sql: str) -> str:
	frappe.only_for("System Manager", "Administrator")
	prepared, err = _prepare_readonly_select(sql)
	if err:
		return json.dumps({"error": err})
	frappe.logger("nce_ai").info(
		"run_readonly_select_query | user={0} | sql={1}".format(frappe.session.user, prepared[:2000])
	)
	try:
		rows = frappe.db.sql(prepared, as_dict=True)
	except Exception as e:
		return json.dumps({"error": str(e), "sql_executed": prepared[:2000]}, default=str)
	out = {"row_count": len(rows), "rows": rows, "sql_executed": prepared}
	return json.dumps(out, default=str)[:MAX_TOOL_OUTPUT_CHARS]


def _openai_tool_definitions():
	return [
		{
			"type": "function",
			"function": {
				"name": "search_frappe_doctypes",
				"description": (
					"Search installed DocType names on this bench site (metadata records named DocType). "
					"Requires permission to read the DocType document. Use a short substring of the name."
				),
				"parameters": {
					"type": "object",
					"properties": {
						"query": {
							"type": "string",
							"description": "Substring to match against DocType name; empty returns the first batch alphabetically.",
						},
					},
					"required": [],
				},
			},
		},
		{
			"type": "function",
			"function": {
				"name": "get_frappe_doctype_schema",
				"description": (
					"Return Frappe field metadata for a DocType: fieldnames, types, options, required flags, "
					"and the SQL table name used for that DocType on this site."
				),
				"parameters": {
					"type": "object",
					"properties": {
						"doctype": {
							"type": "string",
							"description": "Exact DocType name as in Desk, e.g. 'Customer' or 'Sales Order'.",
						},
					},
					"required": ["doctype"],
				},
			},
		},
		{
			"type": "function",
			"function": {
				"name": "describe_frappe_doctype_sql_table",
				"description": (
					"Return SQL column definitions for the database table backing a DocType (e.g. SHOW FULL COLUMNS "
					"on MariaDB / MySQL, or information_schema on PostgreSQL). Read-only."
				),
				"parameters": {
					"type": "object",
					"properties": {
						"doctype": {
							"type": "string",
							"description": "Exact DocType name.",
						},
					},
					"required": ["doctype"],
				},
			},
		},
		{
			"type": "function",
			"function": {
				"name": "run_readonly_select_query",
				"description": (
					"Execute a single read-only SQL query on this site's database and return result rows as JSON. "
					"Allowed: one SELECT or WITH … SELECT only; no writes. A row LIMIT is enforced (max 500). "
					"Only available to users with System Manager or Administrator role. "
					"Use after you know real table/column names from schema tools or user context."
				),
				"parameters": {
					"type": "object",
					"properties": {
						"sql": {
							"type": "string",
							"description": "One SELECT or WITH … SELECT statement. No semicolons except optional trailing.",
						},
					},
					"required": ["sql"],
				},
			},
		},
		{
			"type": "function",
			"function": {
				"name": "create_ai_context_document",
				"description": (
					"Create a new AI Context Document so the user (or you in a later chat) can attach it as reference "
					"in Agent Chat. Requires create permission on AI Context Document. "
					"Use context for long reference text; prompt_injections for text that applies only at the start of a new chat."
				),
				"parameters": {
					"type": "object",
					"properties": {
						"title": {
							"type": "string",
							"description": "Short title for the document list.",
						},
						"context": {
							"type": "string",
							"description": "Optional reference body (sent on every message while selected).",
						},
						"prompt_injections": {
							"type": "string",
							"description": "Optional prompt-injection text (first turn of a new chat only while selected).",
						},
					},
					"required": ["title"],
				},
			},
		},
		{
			"type": "function",
			"function": {
				"name": "update_ai_context_document",
				"description": (
					"Update an existing AI Context Document by its record name (id). Requires write permission. "
					"Only include fields you want to change. Use append_context / append_prompt_injections true to append "
					"instead of replacing."
				),
				"parameters": {
					"type": "object",
					"properties": {
						"document_name": {
							"type": "string",
							"description": "Document name/id (hash) returned when the doc was created or from the list.",
						},
						"title": {"type": "string", "description": "New title (optional)."},
						"context": {"type": "string", "description": "Replace or append context (optional)."},
						"prompt_injections": {
							"type": "string",
							"description": "Replace or append prompt_injections (optional).",
						},
						"append_context": {
							"type": "boolean",
							"description": "If true, append to existing context instead of replacing.",
						},
						"append_prompt_injections": {
							"type": "boolean",
							"description": "If true, append to existing prompt_injections instead of replacing.",
						},
					},
					"required": ["document_name"],
				},
			},
		},
	]


def _tool_search_frappe_doctypes(query: str) -> str:
	if not frappe.has_permission("DocType", "read"):
		return json.dumps(
			{
				"error": (
					"Your user cannot read the DocType list. Ask the user for the exact DocType name, "
					"or use get_frappe_doctype_schema if they name it."
				)
			}
		)
	q = (query or "").strip()
	filters = {}
	if q:
		filters["name"] = ("like", f"%{q}%")
	rows = frappe.get_all(
		"DocType",
		filters=filters,
		fields=["name", "module", "istable", "issingle"],
		limit=100,
		order_by="name asc",
	)
	return json.dumps(rows, default=str)


def _tool_get_frappe_doctype_schema(doctype: str) -> str:
	doctype = (doctype or "").strip()
	if not doctype:
		return json.dumps({"error": "doctype is required"})
	if not frappe.db.exists("DocType", doctype):
		return json.dumps({"error": f"No DocType named {doctype!r}"})
	frappe.has_permission(doctype, "read", throw=True)
	meta = frappe.get_meta(doctype)
	fields_out = []
	for f in meta.fields:
		fields_out.append(
			{
				"fieldname": f.fieldname,
				"label": f.label,
				"fieldtype": f.fieldtype,
				"options": f.options,
				"reqd": int(f.reqd or 0),
				"read_only": int(f.read_only or 0),
				"default": (f.default or "")[:200],
			}
		)
	payload = {
		"doctype": doctype,
		"module": meta.module,
		"table": meta.db_table,
		"istable": int(meta.istable),
		"issingle": int(meta.issingle),
		"is_virtual": int(getattr(meta, "is_virtual", 0) or 0),
		"fields": fields_out[:300],
	}
	if len(fields_out) > 300:
		payload["fields_truncated"] = True
		payload["fields_total"] = len(fields_out)
	return json.dumps(payload, default=str)[:MAX_TOOL_OUTPUT_CHARS]


def _tool_describe_frappe_doctype_sql_table(doctype: str) -> str:
	doctype = (doctype or "").strip()
	if not doctype:
		return json.dumps({"error": "doctype is required"})
	if not frappe.db.exists("DocType", doctype):
		return json.dumps({"error": f"No DocType named {doctype!r}"})
	frappe.has_permission(doctype, "read", throw=True)
	meta = frappe.get_meta(doctype)
	if getattr(meta, "is_virtual", 0):
		return json.dumps({"error": "Virtual DocType — no physical SQL table in the database."})
	if meta.issingle:
		return json.dumps(
			{
				"note": (
					"This is a Single DocType: values are stored as field rows in the `tabSingles` table "
					"(columns include doctype, field, value), not in a dedicated per-DocType table."
				),
				"doctype": doctype,
			}
		)
	table = meta.db_table
	if not table:
		return json.dumps({"error": "Could not resolve SQL table for this DocType."})
	try:
		if frappe.db.db_type in ("mariadb", "mysql"):
			rows = frappe.db.sql(f"SHOW FULL COLUMNS FROM `{table}`", as_dict=True)
		elif frappe.db.db_type == "postgres":
			rows = frappe.db.sql(
				"""
				select column_name as Field, data_type as Type, is_nullable as Null, column_default as Default
				from information_schema.columns
				where table_schema = current_schema() and table_name = %s
				order by ordinal_position
				""",
				(table.lower(),),
				as_dict=True,
			)
		else:
			return json.dumps({"error": f"Unsupported database backend: {frappe.db.db_type!r}"})
	except Exception as e:
		return json.dumps({"error": str(e)})
	return json.dumps(rows, default=str)[:MAX_TOOL_OUTPUT_CHARS]


def _run_frappe_tool(name: str, arguments: dict) -> str:
	try:
		if name == "search_frappe_doctypes":
			return _tool_search_frappe_doctypes(arguments.get("query") or "")
		if name == "get_frappe_doctype_schema":
			return _tool_get_frappe_doctype_schema(arguments.get("doctype") or "")
		if name == "describe_frappe_doctype_sql_table":
			return _tool_describe_frappe_doctype_sql_table(arguments.get("doctype") or "")
		if name == "run_readonly_select_query":
			return _tool_run_readonly_select_query(arguments.get("sql") or "")
		if name == "create_ai_context_document":
			return _tool_create_ai_context_document(arguments or {})
		if name == "update_ai_context_document":
			return _tool_update_ai_context_document(arguments or {})
	except frappe.PermissionError as e:
		return json.dumps({"error": _("Permission denied: {0}").format(str(e))})
	except Exception as e:
		frappe.log_error(frappe.get_traceback(), "nce_ai agent tool error")
		return json.dumps({"error": str(e)})
	return json.dumps({"error": _("Unknown tool: {0}").format(name)})


def _parse_context_doc_names(raw) -> list[str]:
	if not raw:
		return []
	if isinstance(raw, str):
		try:
			raw = json.loads(raw)
		except json.JSONDecodeError:
			return []
	if not isinstance(raw, list):
		return []
	out = []
	for x in raw:
		if isinstance(x, str) and x.strip():
			out.append(x.strip())
	return out


def _truthy_new_session(raw) -> bool:
	if raw is None or raw == "":
		return False
	if isinstance(raw, bool):
		return raw
	if isinstance(raw, (int, float)):
		return raw != 0
	s = str(raw).strip().lower()
	return s in ("1", "true", "yes", "on")


def _prompt_injection_block(names: list[str]) -> str:
	if not names:
		return ""
	parts: list[str] = []
	total = 0
	for name in names:
		doc = frappe.get_doc("AI Context Document", name)
		doc.check_permission("read")
		title = (doc.title or doc.name or "").strip()
		inj = (doc.prompt_injections or "").strip()
		if not inj:
			continue
		chunk = f"### {title}\n(Document id: {doc.name})\n{inj}\n"
		if total + len(chunk) > MAX_PROMPT_INJECTION_CHARS:
			parts.append(
				"\n[Further prompt injections were omitted because the injection section exceeded the size limit.]\n"
			)
			break
		parts.append(chunk)
		total += len(chunk)
	if not parts:
		return ""
	header = (
		"The following instructions were attached for the start of this chat only. "
		"Follow them together with your usual behaviour.\n\n"
	)
	return header + "\n".join(parts)


def _context_appendix_for_docs(names: list[str]) -> str:
	if not names:
		return ""
	parts: list[str] = []
	total = 0
	for name in names:
		doc = frappe.get_doc("AI Context Document", name)
		doc.check_permission("read")
		title = (doc.title or doc.name or "").strip()
		ctx = (doc.context or "").strip()
		if not ctx:
			continue
		chunk = f"### {title}\n(Document id: {doc.name})\n{ctx}\n"
		if total + len(chunk) > MAX_CONTEXT_CHARS:
			parts.append(
				"\n[Further context documents were omitted because the reference section exceeded the size limit.]\n"
			)
			break
		parts.append(chunk)
		total += len(chunk)
	if not parts:
		return ""
	return "\n".join(parts)


@frappe.whitelist()
def list_context_documents():
	"""Documents the current user may read, with flags for the Agent Chat picker."""
	meta_rows = frappe.get_list(
		"AI Context Document",
		fields=["name", "title", "modified"],
		order_by="modified desc",
		limit_page_length=300,
	)
	if not meta_rows:
		return []
	names = [r.name for r in meta_rows]
	if not names:
		return []
	placeholders = ", ".join(["%s"] * len(names))
	flag_rows = frappe.db.sql(
		f"""
		select name,
			(trim(ifnull(`context`, '')) != '') as has_context,
			(trim(ifnull(`prompt_injections`, '')) != '') as has_prompt_injections
		from `tabAI Context Document`
		where name in ({placeholders})
		""",
		tuple(names),
		as_dict=True,
	)
	flag_by_name = {r.name: r for r in flag_rows}
	out = []
	for r in meta_rows:
		f = flag_by_name.get(r.name)
		out.append(
			{
				"name": r.name,
				"title": r.title,
				"modified": r.modified,
				"has_context": bool(f.get("has_context")) if f else False,
				"has_prompt_injections": bool(f.get("has_prompt_injections")) if f else False,
			}
		)
	return out


def _get_provider(provider_name: str | None):
	if provider_name:
		doc = frappe.get_doc("AI Provider", provider_name)
		if not doc.enabled:
			frappe.throw(_("AI Provider is disabled."))
		return doc
	name = frappe.db.get_value("AI Provider", {"enabled": 1}, "name", order_by="modified desc")
	if not name:
		frappe.throw(_("No enabled AI Provider found. Create one under AI Provider."))
	return frappe.get_doc("AI Provider", name)


def _get_default_model(provider):
	for row in provider.models:
		if row.enabled:
			return row
	frappe.throw(_("No enabled model on this AI Provider."))


def _chat_completions_request(url: str, headers: dict, body: dict) -> dict:
	try:
		resp = requests.post(
			url,
			headers=headers,
			json=body,
			timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
		)
	except requests.RequestException as e:
		frappe.throw(_("Could not reach AI provider: {0}").format(str(e)))
	if not resp.ok:
		text = resp.text[:2000] if resp.text else resp.reason
		frappe.throw(_("AI provider error ({0}): {1}").format(resp.status_code, text))
	try:
		return resp.json()
	except json.JSONDecodeError as e:
		frappe.throw(_("Invalid JSON from AI provider: {0}").format(str(e)))


def _run_chat_completion_plain(
	url: str,
	headers: dict,
	model_id: str,
	openai_messages: list[dict],
	max_tokens: int | None,
) -> str:
	body: dict = {"model": model_id, "messages": openai_messages}
	if max_tokens:
		body["max_tokens"] = max_tokens
	data = _chat_completions_request(url, headers, body)
	try:
		msg = data["choices"][0]["message"]
		content = msg.get("content")
		if isinstance(content, str):
			return content
	except (KeyError, IndexError, TypeError) as e:
		frappe.throw(_("Unexpected response from AI provider: {0}").format(str(e)))
	return ""


def _run_chat_with_tools(
	url: str,
	headers: dict,
	model_id: str,
	openai_messages: list[dict],
	max_tokens: int | None,
) -> str:
	tools = _openai_tool_definitions()
	for _round in range(MAX_TOOL_ROUNDS):
		body: dict = {
			"model": model_id,
			"messages": openai_messages,
			"tools": tools,
			"tool_choice": "auto",
		}
		if max_tokens:
			body["max_tokens"] = max_tokens
		if _round == 0:
			try:
				data = _chat_completions_request(url, headers, body)
			except frappe.ValidationError as e:
				err_txt = str(e).lower()
				if "tool" in err_txt or "function" in err_txt:
					return _run_chat_completion_plain(url, headers, model_id, openai_messages, max_tokens)
				raise
		else:
			data = _chat_completions_request(url, headers, body)
		try:
			choice = data["choices"][0]
			msg = choice["message"]
		except (KeyError, IndexError, TypeError) as e:
			frappe.throw(_("Unexpected response from AI provider: {0}").format(str(e)))

		tool_calls = msg.get("tool_calls") or []
		if tool_calls:
			openai_messages.append(
				{
					"role": "assistant",
					"content": msg.get("content"),
					"tool_calls": tool_calls,
				}
			)
			for tc in tool_calls:
				tcid = tc.get("id") or ""
				fn = (tc.get("function") or {}).get("name") or ""
				raw_args = (tc.get("function") or {}).get("arguments") or "{}"
				try:
					args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
					if not isinstance(args, dict):
						args = {}
				except json.JSONDecodeError:
					args = {}
				result = _run_frappe_tool(fn, args)
				openai_messages.append(
					{
						"role": "tool",
						"tool_call_id": tcid,
						"content": result[:MAX_TOOL_OUTPUT_CHARS],
					}
				)
			continue

		content = msg.get("content")
		if isinstance(content, str) and content.strip():
			return content
		if content is None and not tool_calls:
			return ""

	frappe.throw(
		_("The model stopped after too many tool rounds ({0}). Try a narrower question.").format(MAX_TOOL_ROUNDS)
	)


@frappe.whitelist()
def send_agent_message(
	messages,
	provider_name=None,
	context_doc_names=None,
	new_chat_session=None,
):
	"""Exchange one turn with the model. Conversation state stays on the client only."""
	if isinstance(messages, str):
		messages = json.loads(messages)
	if not messages or not isinstance(messages, list):
		frappe.throw(_("Invalid messages."))

	ctx_names = _parse_context_doc_names(context_doc_names)
	new_session = _truthy_new_session(new_chat_session)

	injection_block = _prompt_injection_block(ctx_names) if new_session else ""
	ctx_block = _context_appendix_for_docs(ctx_names)

	system_content = ""
	if injection_block:
		system_content += injection_block + "\n\n---\n\n"
	system_content += SYSTEM_PROMPT
	if ctx_block:
		system_content += (
			"\n\n---\nThe user attached the following reference documents (context). "
			"Treat them as context for this conversation when relevant.\n\n"
		)
		system_content += ctx_block

	provider = _get_provider(provider_name)
	model_row = _get_default_model(provider)
	model_id = (model_row.model_id or model_row.model_name or "").strip()
	if not model_id:
		frappe.throw(_("Model ID / name is missing on the selected model row."))

	api_key = get_decrypted_password("AI Provider", provider.name, fieldname="api_key", raise_exception=False)
	if not api_key:
		frappe.throw(_("API Key is not set on AI Provider."))

	base = (provider.base_url or "").strip().rstrip("/")
	if not base:
		base = "https://api.openai.com"

	url = f"{base}/v1/chat/completions"
	headers = {
		"Authorization": f"Bearer {api_key}",
		"Content-Type": "application/json",
	}

	openai_messages = [{"role": "system", "content": system_content}]
	for m in messages:
		if not isinstance(m, dict):
			continue
		role = m.get("role")
		content = (m.get("content") or "").strip()
		if role in ("user", "assistant") and content:
			openai_messages.append({"role": role, "content": content})

	if len(openai_messages) < 2:
		frappe.throw(_("Send at least one user message."))

	max_out = model_row.max_output_tokens or None

	assistant_text = _run_chat_with_tools(url, headers, model_id, openai_messages, max_out)

	return {"message": assistant_text}

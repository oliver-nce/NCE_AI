import json

import frappe
import requests
from frappe import _
from frappe.utils.password import get_decrypted_password

SYSTEM_PROMPT = """You are a helpful assistant for staff using an ERP-style system.
When the user's task or request is ambiguous, incomplete, or could reasonably go in more than one direction, ask short, specific clarifying questions before doing heavy work or giving a final answer.
If the request is clear enough, answer or act directly without unnecessary questions."""

CONNECT_TIMEOUT = 15
READ_TIMEOUT = 120


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


@frappe.whitelist()
def send_agent_message(messages, provider_name=None):
	"""Exchange one turn with the model. Conversation state stays on the client only."""
	if isinstance(messages, str):
		messages = json.loads(messages)
	if not messages or not isinstance(messages, list):
		frappe.throw(_("Invalid messages."))

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

	openai_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
	for m in messages:
		if not isinstance(m, dict):
			continue
		role = m.get("role")
		content = (m.get("content") or "").strip()
		if role in ("user", "assistant") and content:
			openai_messages.append({"role": role, "content": content})

	if len(openai_messages) < 2:
		frappe.throw(_("Send at least one user message."))

	body: dict = {"model": model_id, "messages": openai_messages}
	if model_row.max_output_tokens:
		body["max_tokens"] = model_row.max_output_tokens

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
		data = resp.json()
		assistant_text = data["choices"][0]["message"]["content"]
	except (KeyError, IndexError, TypeError) as e:
		frappe.throw(_("Unexpected response from AI provider: {0}").format(str(e)))

	return {"message": assistant_text}

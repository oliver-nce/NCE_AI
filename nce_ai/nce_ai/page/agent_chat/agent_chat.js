frappe.pages["agent-chat"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Agent Chat"),
		single_column: true,
	});

	const $main = $(page.main);
	$main.empty();

	const messages = [];
	let newChatSession = true;

	const $thread = $(`<div class="nce-agent-chat-thread" style="display:flex;flex-direction:column;gap:12px;padding:8px 0 16px;max-height:calc(100vh - 320px);overflow-y:auto;"></div>`);
	const $inputRow = $(`
		<div class="nce-agent-chat-input form-group" style="margin-top:8px;">
			<details class="nce-agent-chat-context" style="margin-bottom:12px;">
				<summary style="cursor:pointer;font-weight:500;">${__("Reference documents (optional)")}</summary>
				<p class="text-muted small" style="margin:6px 0 0;">${__(
					"Context is sent every turn. Prompt injections apply only on the first message after you open or clear the chat, while the document stays selected."
				)}</p>
				<div class="nce-agent-chat-context-inner" style="margin-top:8px;padding:8px;border:1px solid var(--border-color);border-radius:6px;max-height:200px;overflow-y:auto;"></div>
				<div style="margin-top:6px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;">
					<button type="button" class="btn btn-default btn-xs nce-agent-chat-ctx-refresh">${__("Refresh list")}</button>
					<button type="button" class="btn btn-default btn-xs nce-agent-chat-ctx-open-list">${__("Open list")}</button>
				</div>
			</details>
			<label class="control-label">${__("Your message")}</label>
			<textarea class="form-control nce-agent-chat-textarea" rows="3" placeholder="${__("Describe a task; the agent may ask clarifying questions.")}"></textarea>
			<div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap;">
				<button type="button" class="btn btn-primary btn-sm nce-agent-chat-send">${__("Send")}</button>
				<button type="button" class="btn btn-default btn-sm nce-agent-chat-clear">${__("Clear chat")}</button>
			</div>
		</div>
	`);

	$main.append($thread);
	$main.append($inputRow);

	const $ta = $inputRow.find(".nce-agent-chat-textarea");
	const $ctxInner = $inputRow.find(".nce-agent-chat-context-inner");

	function getSelectedContextNames() {
		const names = [];
		$ctxInner.find(".nce-ctx-doc:checked").each(function () {
			const n = $(this).attr("data-name");
			if (n) names.push(n);
		});
		return names;
	}

	async function loadContextList() {
		const preserve = new Set(getSelectedContextNames());
		$ctxInner.empty().append(`<p class="text-muted small">${__("Loading…")}</p>`);
		try {
			const r = await frappe.call({
				method: "nce_ai.api.agent_chat.list_context_documents",
			});
			const rows = r.message || [];
			$ctxInner.empty();
			if (!rows.length) {
				$ctxInner.append(
					`<p class="text-muted small">${__("No AI Context Documents yet. Create one from the list.")}</p>`
				);
				return;
			}
			for (const row of rows) {
				const name = row.name;
				const title = row.title || name;
				const tags = [];
				if (row.has_context) tags.push(__("context"));
				if (row.has_prompt_injections) tags.push(__("prompt injection"));
				const tagStr = tags.length ? ` (${tags.join(", ")})` : "";
				const $row = $(`<div class="checkbox" style="margin:4px 0;"></div>`);
				const $label = $(`<label style="margin:0;font-weight:normal;"></label>`).appendTo($row);
				$("<input>", {
					type: "checkbox",
					class: "nce-ctx-doc",
					"data-name": name,
					checked: !!preserve.has(name),
				}).appendTo($label);
				$label.append(document.createTextNode(` ${title}`));
				if (tagStr) {
					$label.append(
						$("<span>").addClass("text-muted").css({ "font-size": "11px" }).text(tagStr)
					);
				}
				$ctxInner.append($row);
			}
		} catch {
			$ctxInner.empty().append(`<p class="text-danger small">${__("Could not load context documents.")}</p>`);
		}
	}

	$inputRow.find(".nce-agent-chat-ctx-refresh").on("click", () => loadContextList());
	$inputRow.find(".nce-agent-chat-ctx-open-list").on("click", () => {
		frappe.set_route("List", "AI Context Document");
	});

	loadContextList();

	function append_bubble(role, text) {
		const align = role === "user" ? "flex-end" : "flex-start";
		const bg = role === "user" ? "var(--control-bg)" : "var(--bg-color)";
		const color = "var(--text-color)";
		const label = role === "user" ? __("You") : __("Agent");
		const $b = $(`
			<div style="display:flex;flex-direction:column;align-items:${align};max-width:100%;">
				<div style="font-size:11px;color:var(--text-muted);margin-bottom:4px;">${frappe.utils.escape_html(label)}</div>
				<div class="nce-agent-chat-bubble" style="max-width:min(720px,100%);padding:10px 14px;border-radius:8px;background:${bg};color:${color};white-space:pre-wrap;word-break:break-word;border:1px solid var(--border-color);">${frappe.utils.escape_html(text)}</div>
			</div>
		`);
		$thread.append($b);
		$thread.scrollTop($thread[0].scrollHeight);
	}

	function render_error(msg) {
		append_bubble("assistant", `${__("Error")}: ${msg}`);
	}

	function set_busy(busy) {
		$inputRow.find("button").prop("disabled", busy);
		$ta.prop("disabled", busy);
		$ctxInner.find("input").prop("disabled", busy);
	}

	async function send() {
		const text = ($ta.val() || "").trim();
		if (!text) {
			frappe.show_alert({ message: __("Enter a message."), indicator: "orange" });
			return;
		}

		messages.push({ role: "user", content: text });
		append_bubble("user", text);
		$ta.val("");

		set_busy(true);
		const sendAsNewChat = newChatSession;
		try {
			const r = await frappe.call({
				method: "nce_ai.api.agent_chat.send_agent_message",
				args: {
					messages: JSON.stringify(messages),
					context_doc_names: JSON.stringify(getSelectedContextNames()),
					new_chat_session: sendAsNewChat ? 1 : 0,
				},
			});
			const assistant = r.message?.message;
			if (!assistant) {
				render_error(__("Empty response."));
				messages.pop();
				return;
			}
			newChatSession = false;
			messages.push({ role: "assistant", content: assistant });
			append_bubble("assistant", assistant);
		} catch (e) {
			messages.pop();
			let err = e?.message || String(e);
			if (e?._server_messages) {
				try {
					const parsed = JSON.parse(e._server_messages);
					const first = parsed[0] ? JSON.parse(parsed[0]) : null;
					if (first?.message) err = first.message;
				} catch {
					/* use err as-is */
				}
			}
			render_error(err);
		} finally {
			set_busy(false);
			$ta.focus();
		}
	}

	$inputRow.find(".nce-agent-chat-send").on("click", () => send());
	$inputRow.find(".nce-agent-chat-clear").on("click", () => {
		messages.length = 0;
		newChatSession = true;
		$thread.empty();
		$ta.val("");
		$ta.focus();
	});

	$ta.on("keydown", (ev) => {
		if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) {
			ev.preventDefault();
			send();
		}
	});

	append_bubble(
		"assistant",
		__(
			"Give me a task in plain language. If something is unclear, I will ask a few clarifying questions before proceeding."
		)
	);
	$ta.focus();
};

frappe.pages["agent-chat"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Agent Chat"),
		single_column: true,
	});

	const $main = $(page.main);
	$main.empty();

	const messages = [];

	const $thread = $(`<div class="nce-agent-chat-thread" style="display:flex;flex-direction:column;gap:12px;padding:8px 0 16px;max-height:calc(100vh - 220px);overflow-y:auto;"></div>`);
	const $inputRow = $(`
		<div class="nce-agent-chat-input form-group" style="margin-top:8px;">
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
		try {
			const r = await frappe.call({
				method: "nce_ai.api.agent_chat.send_agent_message",
				args: { messages: JSON.stringify(messages) },
			});
			const assistant = r.message?.message;
			if (!assistant) {
				render_error(__("Empty response."));
				messages.pop();
				return;
			}
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

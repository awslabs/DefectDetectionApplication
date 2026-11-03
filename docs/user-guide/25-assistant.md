# 25. The assistant

**Who:** every signed-in Portal user except accounts whose only role is DataLabeler; each look-up the assistant makes runs with your own permissions, so it can see only what you can see in the web UI. **Reference:** [Administrator Guide, Portal assistant](../../edge-cv-portal/ADMIN_GUIDE.md#portal-assistant), [24. MCP support](24-mcp-support.md).

The assistant is a chat panel on every Portal page. Ask it what a page or a field does, where you are on the path from a new use case to a running station, or to look something up for you ("which devices are offline?", "what is the status of the last training job?"). It answers from this user guide and from the Portal's own data, using the same tool catalog an MCP client such as Kiro uses ([24.4](24-mcp-support.md#244-what-the-tools-can-read)), as you, with your permissions.

## 25.1 Opening and hiding the panel

The assistant lives in the drawer bar at the right edge of the page, behind the sparkle icon labelled **Open the assistant**. The first time you sign in on a wide screen the panel is open; on the workflow builder and on narrow screens it starts closed. Close it with **Hide the assistant** (the × in its header) and open it again from the drawer bar. The Portal remembers your choice in this browser, on every page, until you change it. On narrow screens the panel opens over the page; press Escape or the close control to dismiss it. Drag the panel's left edge to resize it.

While the panel is closed, a dot on its icon tells you that a response or a confirmation is waiting.

## 25.2 Asking

Type in the box at the bottom (up to 4,000 characters) and press Enter or the send arrow. An empty conversation offers suggested prompts for the page you are on; selecting one sends it. The assistant knows which page you are on and which use case is selected, so "what does this field do?" and "how many images does this use case have?" need no further context. It does not see what you typed into the page's forms.

While it works you see what it does: each look-up appears as a line in the response ("List devices (usecase_id=cookies): done · 1.2 s"), and the text appears as it is produced. **Stop** ends the turn; whatever was produced so far stays in the conversation.

Responses cite the user guide section they used as a link such as "§ 15.2 Registering a device"; the link opens the chapter. Portal pages are linked by name and open in the Portal.

## 25.3 What the assistant can and cannot do

| It can | It cannot |
|--------|-----------|
| Explain pages, fields, values and procedures from this guide | Invent fields or procedures; when the guide and the Portal's data do not answer, it says so |
| Tell you the next step on the end-to-end path for the selected use case, from what that use case already has | See another user's use cases, or anything your role cannot read |
| Look things up through the Portal's tool catalog: devices, deployments, models, training jobs, workflows, builds, labeling jobs, datasets, audit logs (PortalAdmin) | Change anything in this release: creating, deploying, deleting and other state changes are not available through the assistant yet. Ask it where to do it and it opens the page for you |
| Keep your conversations, so you can continue later or on another browser | Answer questions unrelated to operating DDA |

When an administrator enables **actions** in a later release, a state change the assistant proposes appears as a card naming the tool, every argument and the use case; nothing happens until you confirm that card, and a confirmation is valid for ten minutes and for that exact proposal only.

## 25.4 Conversations

The **+** control starts a new conversation; the clock control lists your earlier ones, with reopen and delete. Conversations belong to you: no other user, PortalAdmins included, can read them. They are kept for the retention period the administrator sets (30 days of inactivity by default) and then deleted. Deleting a conversation removes its content at once; the audit log keeps the record of the look-ups the assistant made on your behalf ([25.5](#255-the-assistant-in-the-audit-logs)).

A conversation holds up to 60 turns; after that, start a new one.

## 25.5 The assistant in the Audit Logs

Every look-up the assistant performs is written to the Audit Logs like a Portal action, under the channel **Assistant** ([21. Audit Logs](21-audit-logs.md)). The entry names the tool, the use case and you as the acting user, and carries the turn id, so an administrator can tell assistant activity from your own clicks and from external MCP clients. Your prompts and the assistant's answers are not in the audit log.

## 25.6 Limits and errors

| Message | Meaning | What to do |
|---------|---------|------------|
| "You have reached the … limit" | Each user has a number of turns per hour and of model tokens per day | Wait for the time shown, or ask an administrator to raise the limits |
| "This conversation is full" | The conversation reached 60 turns or its size limit | Start a new conversation |
| "I stopped here because the turn reached its … limit" | One turn may make a bounded number of model calls and look-ups | Ask a follow-up to continue |
| "The model is busy (throttled)" | Bedrock refused the call for the moment | Try again shortly |
| "The configured model is not available" or "not allowed to call the configured model" | The Bedrock configuration points to a model this account cannot use | Ask a PortalAdmin to check Settings → Bedrock Configuration |
| "The assistant did not finish" | The turn's worker stopped unexpectedly | Try again; if it repeats, tell an administrator |
| The panel is gone | A PortalAdmin turned the assistant off, or your account's only role is DataLabeler | Nothing to do; the page works as before |

The assistant's answers are generated by a model. Check them before acting on them.

# 25. The assistant

**Who:** every signed-in Portal user except accounts whose only role is DataLabeler; each look-up and each change the assistant makes runs with your own permissions, so it can see and do only what you can in the web UI. **Video:** [Module 14: MCP support](https://youtu.be/8CWPi96EpcY). **Reference:** [Administrator Guide, Portal assistant](../../edge-cv-portal/ADMIN_GUIDE.md#portal-assistant), [24. MCP support](24-mcp-support.md).

The assistant is a chat panel on every Portal page. Ask it what a page or a field does, where you are on the path from a new use case to a running station, to look something up for you ("which devices are offline?", "what is the status of the last training job?"), or to make a change for you ("create a labeling team named night shift"), which you confirm first. It answers from this user guide and from the Portal's own data, using the same tool catalog an MCP client such as Kiro uses ([24.4](24-mcp-support.md#244-what-the-tools-can-read)), as you, with your permissions.

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
| Look things up through the Portal's tool catalog: devices, deployments, models, training jobs, workflows, builds, labeling jobs, datasets, audit logs (PortalAdmin) | Delete, cancel, stop, remove or deploy anything, or make another change the catalog marks destructive (such as promoting a plugin): those stay on the Portal's pages. Ask it where to do one and it links the page |
| Make other changes for you through the same catalog, such as creating a labeling team, a workflow or a camera, each one only after you confirm it | Change anything without your confirmation, or while an administrator has turned **Actions** off |
| Keep your conversations, so you can continue later or on another browser | Answer questions unrelated to operating DDA |

A change the assistant proposes appears as a card naming the tool, every argument and the use case it applies to; nothing happens until you select **Confirm** on that card. **Decline** tells the assistant not to do it, and it does not propose the same change again in that turn. Each change has its own card, so a request for two changes shows two cards. A confirmation is valid for ten minutes and for that exact proposal only; an unanswered card expires and the turn ends. After a change, the assistant reports what it created or changed and can check its status for you. With **Actions** turned off in Settings, the assistant only looks things up and points you to the page where you make the change.

## 25.4 Conversations

The **+** control starts a new conversation; the clock control lists your earlier ones, with reopen and delete. Conversations belong to you: no other user, PortalAdmins included, can read them. They are kept for the retention period the administrator sets (30 days of inactivity by default) and then deleted. Deleting a conversation removes its content at once; the audit log keeps the record of the look-ups and changes the assistant made on your behalf ([25.5](#255-the-assistant-in-the-audit-logs)).

A conversation holds up to 60 turns; after that, start a new one.

## 25.5 The assistant in the Audit Logs

Every look-up and every confirmed change the assistant performs is written to the Audit Logs like a Portal action, under the channel **Assistant** ([21. Audit Logs](21-audit-logs.md)); a declined or expired card runs nothing. The entry names the tool, the use case and you as the acting user, and carries the turn id, so an administrator can tell assistant activity from your own clicks and from external MCP clients. Your prompts and the assistant's answers are not in the audit log.

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

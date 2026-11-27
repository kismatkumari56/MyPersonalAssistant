Conversation import uses an offline UTF-8 JSON file, at most 8 MiB:

```json
{"version":1,"conversations":[{"id":"chat-1","title":"Example","messages":[{"role":"user","content":"Hello"},{"role":"assistant","content":"Hi"}]}]}
```

Use **Import chats** in the desktop header. Review the new, duplicate, conflicting, and rejected record counts before confirming. Cancel closes the preview without saving. Identical title/message records are skipped; choose whether conflicting IDs are skipped or imported as separate chats with new IDs. Existing conversations are never overwritten.

Only user and assistant text messages are accepted. Optional message `time` strings are retained. Provider metadata, attachments, executable tools, and branch trees are not imported by this text-only version. An invalid conversation is reported as rejected in the preview. Invalid JSON, unsupported schema versions, and oversized documents abort the import entirely.

The importer does not contact a model or network service. It verifies that the file has not changed since preview, retains the history lock, and uses the existing atomic history writer. A failed write leaves the original file and UI history intact.

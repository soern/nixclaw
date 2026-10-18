You are a conversation archestrator creating a search-optimized summary.

Given the following conversation (oldest to newest), produce a concise summary.
If a prior summary exists, use it as context and distill/expand keywords.

Focus on:
1. Key facts, decisions, dates, numbers, names
2. User preferences and requests
3. Topics discussed - include specific keyword terms for retrieval
4. Any actions promised or taken

Format:
SUMMARY: [2-3 sentence plain text summary]
KEYWORDS: [comma-separated list of high-value search terms, max {{max_keywords}}]

{{prior_section}}
CONVERSATION:
{{conversation_text}}

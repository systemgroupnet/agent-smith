# LangSmith Quick Start

Get LangSmith tracing up and running in 5 minutes!

## 1. Get API Key (2 minutes)

1. Visit: https://smith.langchain.com/
2. Sign up (free, no credit card)
3. Go to: Settings → API Keys → Create API Key
4. Copy your key (starts with `lsv2_pt_...`)

## 2. Update `.env` (1 minute)

Open your `.env` file and find these lines:

```env
# LangSmith Configuration
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=your_langsmith_api_key_here
LANGSMITH_PROJECT=codereviewagent
LANGSMITH_ENDPOINT=https://api.smith.langchain.com
```

Replace `your_langsmith_api_key_here` with your actual key:

```env
LANGSMITH_API_KEY=lsv2_pt_abc123...
```

Tracing is fully env-driven — LangChain reads these variables automatically, no code
changes required.

## 3. Run a Review (1 minute)

```bash
python main.py --gitlab-mr "YOUR_MR_URL"
```

Look for the thread id printed at the start of the review:

```
🧵 LangSmith thread: group/project!123
```

## 4. View Traces

1. Go to: https://smith.langchain.com/
2. Click on the "codereviewagent" project
3. Open the **Threads** tab — one thread per MR review! 🎉

## What You'll See

Each merge request review is grouped into one **thread** (keyed by the MR), containing:

- 📄 One trace per file reviewed (`review:<path>`)
- 🤖 Which agent reviewed it, plus its LLM / MCP tool calls
- 🔍 A `comment_review` trace for the final filtering step
- ⏱️ How long it took
- 💰 How much it cost (tokens)
- ✅ What issues were found

Re-reviewing the same MR appends new traces to the same thread.

## Troubleshooting

### "API key not set"

Make sure you saved the `.env` file after adding your key.

### "Cannot connect"

Check your internet connection and verify your API key is correct.

### "No traces appearing"

Wait 10-15 seconds, then refresh the LangSmith page.

## Learn More

- **Full Guide**: `docs/LANGSMITH_SETUP.md`
- **Changes Made**: `LANGSMITH_CHANGES.md`
- **LangSmith Docs**: https://docs.smith.langchain.com/

---

That's it! You're now tracing your code reviews. 🚀

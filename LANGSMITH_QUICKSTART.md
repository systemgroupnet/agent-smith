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
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=your_langsmith_api_key_here
LANGCHAIN_PROJECT=codereviewagent
```

Replace `your_langsmith_api_key_here` with your actual key:

```env
LANGCHAIN_API_KEY=lsv2_pt_abc123...
```

## 3. Test Setup (1 minute)

```bash
python scripts/test_langsmith.py
```

You should see:

```
✅ LANGCHAIN_TRACING_V2 is enabled
✅ LANGCHAIN_API_KEY is set: lsv2_pt_...xyz
✅ LANGCHAIN_PROJECT is set: codereviewagent
✅ Successfully connected to LangSmith
```

## 4. Run a Review (1 minute)

```bash
python main.py --gitlab-mr "YOUR_MR_URL"
```

Look for this at the start:

```
🔍 LangSmith tracing enabled
📊 Project: codereviewagent
```

## 5. View Traces

1. Go to: https://smith.langchain.com/
2. Click on "codereviewagent" project
3. See your traces! 🎉

## What You'll See

Each merge request creates a trace showing:

- 📄 Every file reviewed
- 🤖 Which agent reviewed it
- ⏱️ How long it took
- 💰 How much it cost (tokens)
- ✅ What issues were found

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

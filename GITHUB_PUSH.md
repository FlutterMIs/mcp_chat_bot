# GitHub par upload karne ka guide

Project already ek local git repo hai (`main` branch, 15 commits, tags `pre-agentic-baseline` … `phase4-response-policy`).
Sirf remote jodna aur push karna baaki hai.

## Kya push hoga, kya nahi

`.gitignore` ki wajah se ye **kabhi** upload nahi honge:

| Ignored | Kyun |
|---|---|
| `.env` | OpenRouter key, WhatsApp secret, sheet URL |
| `workspace_memory.json` | tumhare learned rules / column choices |
| `whatsapp_state.db*`, `whatsapp_files/`, `whatsapp_shared/`, `whatsapp_debug.json` | chat state, media |
| `llm_usage.jsonl` | token/cost log |
| `.venv/`, `__pycache__/`, `.pytest_cache/` | local environment |

Jo push hoga: code, tests, `README.md`, `audit/`, `.env.example` (bina values ke), `demo.db` (8 KB sample table).
Push se pehle check: `git ls-files | grep -E "\.env$|workspace_memory"` → kuch nahi aana chahiye.

## Step 1 — GitHub par repo banao

1. https://github.com/new
2. Repository name: `business-analyst-bot` (ya jo chaho)
3. **Private** select karo
4. "Add a README" / ".gitignore" / "license" — **kuch mat tick karo** (repo empty rehna chahiye)
5. **Create repository**

## Step 2 — Authentication (ek option chuno)

### Option A — SSH (recommended)

Public key copy karo:

```bash
cat ~/.ssh/id_ed25519.pub
```

GitHub → Settings → **SSH and GPG keys** → **New SSH key** → paste → Add.

Test:

```bash
ssh -T git@github.com
# expected: "Hi <username>! You've successfully authenticated…"
```

Remote URL format: `git@github.com:<username>/business-analyst-bot.git`

### Option B — HTTPS + token

GitHub → Settings → Developer settings → **Personal access tokens** → Generate (scope: `repo`).
Push karte waqt username + ye token password ki jagah daalna. (Token kisi ko mat bhejna, chat mein bhi nahi.)

Remote URL format: `https://github.com/<username>/business-analyst-bot.git`

## Step 3 — Push

```bash
cd ~/Downloads/mcp_openrouter_chatbot

# remote jodo (apna URL daalo — SSH ya HTTPS)
git remote add origin git@github.com:<username>/business-analyst-bot.git

# pehli baar: branch + saare tags
git push -u origin main
git push origin --tags
```

## Step 4 — Verify

```bash
git remote -v          # origin dikhna chahiye
git status             # "Your branch is up to date with 'origin/main'"
```

GitHub par repo kholo → files dikhni chahiye, `.env` **nahi** dikhna chahiye.

## Aage se (har change ke baad)

```bash
git add -A
git commit -m "kya badla"
git push
```

## Dusri machine / server par clone karke chalana

```bash
git clone git@github.com:<username>/business-analyst-bot.git
cd business-analyst-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # phir .env mein apni keys bharo
streamlit run app.py
```

## Problems

| Error | Matlab / fix |
|---|---|
| `Permission denied (publickey)` | SSH key GitHub par add nahi hui — Step 2A |
| `remote origin already exists` | `git remote set-url origin <url>` |
| `Updates were rejected` | repo empty nahi tha — `git pull --rebase origin main` phir push |
| `Host key verification failed` | `ssh-keyscan github.com >> ~/.ssh/known_hosts` |

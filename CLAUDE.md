# CLAUDE.md — is project par kaam shuru karne se pehle

1. **Pehle `PROJECT_STATE.md` padho** — architecture, files, rules, pending items, testing pattern sab wahan hai.
2. User Hinglish mein baat karta hai; jawab bhi Hinglish mein, seedhe, bina lambe recap ke.
3. **OpenRouter spend minimum**: tests offline (`NoLLM` pattern, `PROJECT_STATE.md` §7). Live/golden runs sirf user ke kehne par.
4. Kabhi number guess mat karo; generic raho (koi business column name hardcode nahi); secrets print/commit nahi.
5. Har fix ke baad: `.venv/bin/python -m pytest tests -q -p no:cacheprovider -W ignore` (sab pass) + real sheet par
   offline verify, tab "ho gaya" bolo. Test runs ke baad `workspace_memory.json` mein test mappings mat chhodo.
6. Commit karo (short message, `Co-Authored-By` line ke saath); bade milestone par tag. `PROJECT_STATE.md` update karo.

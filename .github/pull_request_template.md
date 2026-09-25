## Module

Which module does this change belong to (for example `feature/01-telegram-bot`)?

## Summary

What changed and why.

## Checklist

- [ ] Module named above and the change stays inside its spec
- [ ] Tests added or updated for the new behaviour
- [ ] CI green (`ruff check .`, `pytest`, smoke test)
- [ ] No real emails or tokens in the diff
- [ ] No long dashes (em dash or en dash) anywhere
- [ ] `DRY_RUN` default unchanged (true unless exactly `false`)
- [ ] If this touches Notion writes or mail, `safety.py` is used

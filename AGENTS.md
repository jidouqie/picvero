# Project guidance

- Read README.md and 项目思路.md for scope. This is a Codex-native ecommerce skill package, not a hosted image service.
- The product name and prefix are in project.json. The main skill uses the bare prefix (`picvero`); other skills use `prefix-capability`. build.py renders names and cross-references, including the `{{ENTRY}}` placeholder for the main skill.
- Edit skill_sources/ and shared/, then run `python3 tools/build.py`. skills/ is generated and self-contained per skill; do not hand-edit it.
- Image creation and editing belong to the active Codex session's built-in image tool. Do not add provider API clients, credential discovery, external generation CLIs, or an author-hosted proxy.
- Local scripts manage files, evidence, state, checks, and delivery. Missing optional local helpers must not imply a missing native image capability.
- Preserve the separation between generated output, reviewed candidate, and explicit user selection. Unknown checks must remain unknown.
- Each product needs its own references and fact provenance. Keep original references available during revisions; style references cannot supply product facts.
- Run `python3 -m unittest discover -s tests -v` for runtime/package changes. Run the available official skill validator for skill metadata changes. Keep validation-only dependencies out of runtime packages.
- The included HY-17 images are synthetic test fixtures. Do not present them as real merchant results, measured fidelity, platform approval, or sales evidence.
- Publish or install outside this repository only within the user's requested scope. The local `.agents/skills` links may point at this repository's generated skills.

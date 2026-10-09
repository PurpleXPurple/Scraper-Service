# LICENSE

**nexus — single-pass scraper service**
**Version 1.2.0 · October 2026**

This document governs the use, modification, and distribution of the nexus project. It is MIT-based with four additional clauses that apply to this specific project. Read all five sections before you use, fork, or deploy anything here.

---

## 1 · MIT License (the base)

Copyright © 2026, the nexus project contributors.

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions.

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

---

## 2 · Additional Clause A — Attribution

> [!important] This clause adds to the MIT terms above.

If you use, fork, redistribute, or deploy any part of this project — including derivative works, hosted services, and bundled distributions — you must do all of the following.

### A.1 Preserve the copyright

Keep the copyright line intact in every source file you copy or fork. Do not strip headers, comments that reference this license, or the project name.

### A.2 Credit the original in your documentation

In your README, about page, footer, or legal notice, include a clear line that credits the nexus project. Any of the following is acceptable.

- A visible line such as `Powered by nexus` or `Built on nexus`.
- A link to the original repository.
- A `NOTICE` file that names nexus as the upstream project.

The credit does not need to be prominent. It only needs to be present and honest.

### A.3 Credit the structure, not only the code

If you reuse the architectural patterns described in this project — the three-file layout, the single-pass pipeline, the mode-selection system, the RTL shaping layer, the declarative Config.json approach, or the graph rendering strategy — give credit to the structure as well as to the code. Two sentences in your README are enough:

> The three-file architecture (Scraper.py / index.html / Config.json) is adapted from the nexus project.
> The RTL shaping pipeline follows the approach documented in nexus.

Credit for structure is not required for trivial use. If you copied one utility function, one CSS class, or one regex pattern, a general mention is fine. If you reproduced the whole pipeline, credit it as such.

### A.4 Preserve upstream license files

If you redistribute the Software in any form, include this `LICENSE` file (or an unmodified copy) alongside your distribution.

---

## 3 · Additional Clause B — Code license built into the project

> [!note] This clause clarifies the internal licensing structure.

The three primary files of this project carry the same license and the same terms. There is no per-file exception.

| File | Role | License |
|---|---|---|
| `Scraper.py` | Server runtime | MIT + Clause A, B, C, D |
| `index.html` | Client interface | MIT + Clause A, B, C, D |
| `Config.json` | Declarative configuration | MIT + Clause A, B, C, D |

If you add a new file to your fork, you may license it however you wish, provided that it does not modify, replace, or remove the obligations listed in Sections 2, 4, and 5 of this document.

### B.1 Third-party dependencies

These libraries are not owned by this project. They carry their own licenses. Your use of nexus implies your acceptance of their terms.

| Library | License | Use |
|---|---|---|
| FastAPI | MIT | HTTP framework |
| Uvicorn | BSD-3-Clause | ASGI server |
| Pydantic | MIT | Request validation |
| aiosqlite | MIT | Async SQLite driver |
| argon2-cffi | MIT | Password hashing |
| cryptography | Apache-2.0 / BSD-3-Clause | AES-GCM, primitives |
| curl_cffi | MIT | HTTP client with impersonation |
| selectolax | MIT | HTML parser |
| Pillow | HPND | Image rendering |
| pymupdf | AGPL-3.0 (see B.2) | PDF extraction |
| zstandard | BSD-3-Clause | Cache compression |
| arabic-reshaper | MIT | Arabic shaping |
| python-bidi | LGPL-2.1 | Bidi reordering |
| orjson | MIT | Fast JSON |

> [!warning] PyMuPDF is AGPL-3.0
> If you distribute a network service built on nexus and you have not separately licensed PyMuPDF, you are bound by the AGPL's network-copyleft clause: your entire service source must be offered to users. To avoid this, either remove PyMuPDF support, purchase an Artifex commercial license, or replace it with `pdfminer.six` (MIT) or `pypdf` (BSD).

### B.2 Fonts

The bundled or referenced fonts also carry licenses.

| Font | License | Scope |
|---|---|---|
| Inter | SIL Open Font License 1.1 | UI, report |
| Space Grotesk | SIL Open Font License 1.1 | Display, brand |
| JetBrains Mono | SIL Open Font License 1.1 | Code, labels |
| Noto Naskh Arabic | SIL Open Font License 1.1 | Arabic report |
| Noto Sans Hebrew | SIL Open Font License 1.1 | Hebrew report |

SIL OFL permits free use, modification, and redistribution, including in commercial products, provided you do not sell the fonts by themselves and you preserve the license file.

### B.3 Icon fonts

Font Awesome Free is licensed under CC BY 4.0 (icons) and MIT (code). Commercial use is permitted with attribution. The client already ships the required attribution in the HTML comment header.

---

## 4 · Additional Clause C — Acceptable use and legal compliance

> [!danger] Read this section in full.
> This project is a tool. The tool itself is legal. How you use it may not be.

### C.1 Authorized targets only

You may only use this software against targets you own or have explicit written permission to access. This includes:

- Your own websites and APIs.
- Websites you have been contracted to audit or monitor.
- Public data that is unambiguously authorized for scraping by the site's own terms, robots.txt, or an explicit API agreement.
- Educational and research settings where authorization is documented.

### C.2 Prohibited use

You may not use this software to:

- Evade, bypass, or defeat any authentication, paywall, rate limit, access control, or anti-bot system on a site you do not own.
- Scrape personal data, private messages, health records, financial records, or content protected by a reasonable expectation of privacy.
- Violate the Computer Fraud and Abuse Act (US), the Computer Misuse Act (UK), the GDPR, the CCPA, or any equivalent statute in your jurisdiction.
- Impersonate a specific real person.
- Distribute malware, spam, phishing content, or CSAM.
- Build a competing service that scrapes a site in violation of that site's terms.
- Bypass the access controls of any API, including GitHub's, Hugging Face's, or any search engine's.

### C.3 robots.txt and site policies

This software reads `robots.txt` when configured to do so. You are responsible for ensuring that your deployment respects it. The default configuration is conservative. Do not disable it unless you have a documented legal basis.

### C.4 Rate limiting and civility

The default configuration rate-limits per IP. Do not raise these limits for sites you do not own. Do not disable cooldowns. Do not run the pipeline in tight loops against a single domain. A scraper that behaves badly is not a scraper that survives.

### C.5 Personal data

If you enable `NEXUS_STORE_EXACT_IP=1`, you are storing personal data. Under GDPR, this requires:

- A documented lawful basis (typically legitimate interest for abuse prevention).
- A retention policy (the default is 30 days).
- A right-to-erasure process for data subjects.
- A record of processing activities.

The default mode stores an HMAC of the IP address, not the address itself. This is a privacy-preserving design. Do not change it without legal review.

### C.6 Downstream responsibility

If you fork this project, host it as a service, or distribute it to others, you become the data controller for any personal data your service processes. The original authors of nexus are not responsible for how you deploy it.

### C.7 No legal advice

This document is not legal advice. If your use case falls outside the obvious scope of authorized scraping, consult a lawyer in your jurisdiction before proceeding.

---

## 5 · Additional Clause D — Contribution terms

> [!note] Applies if you contribute code, docs, or assets back to this project.

By submitting a pull request, patch, issue with attached code, or any other form of contribution to the nexus project, you agree to the following.

### D.1 Your contribution is licensed under this same license

You grant the project a perpetual, worldwide, royalty-free, non-exclusive license to use, reproduce, modify, and distribute your contribution under the terms of this document.

### D.2 You have the right to contribute

You confirm that the contribution is your original work, or that you have the legal right to submit it under this license. If your contribution incorporates third-party code, you identify that code and its license in the pull request.

### D.3 No warranty from contributors

You provide your contribution "as is" and make no warranty about its fitness for any purpose. The MIT warranty disclaimer in Section 1 applies.

### D.4 Attribution to contributors

Your name or handle will be preserved in the commit history. If you want explicit credit in a `CONTRIBUTORS.md` file, add yourself to it in the same pull request.

---

## 6 · How to use this project properly — operator checklist

> [!tip] A practical walkthrough for someone about to deploy nexus.

**Step 1 — Verify the license fits your use case.**

- Personal or internal use: no action needed beyond keeping this file in your distribution.
- Commercial service: read Clause A carefully. Credit the project. If you use PyMuPDF, resolve the AGPL question before you launch.
- Redistribution or fork: preserve this file. Credit the project. Do not strip the header comments.

**Step 2 — Verify your targets are authorized.**

- Make a list of every domain your deployment will fetch.
- For each one, confirm robots.txt allows your user agent.
- For each one, confirm the site's terms of service do not prohibit scraping.
- For each one you cannot confirm, either remove it or document the legal basis.

**Step 3 — Configure responsibly.**

- Leave `NEXUS_STORE_EXACT_IP=0` unless you have a documented legal basis for storing exact IPs.
- Leave `rate_limit` at its defaults for sites you do not own.
- Set `GITHUB_TOKEN` and `HF_TOKEN` if you use those clients, so you do not exhaust anonymous quotas.

**Step 4 — Back up secrets.**

- `var/secrets.json` is generated on first run and holds the AES key, JWT secret, and HMAC key.
- If you lose it, cached entries become unreadable and existing JWTs become invalid.
- Store it in an encrypted backup before your first production run.

**Step 5 — Prepare for compliance questions.**

- Retain logs for the period your jurisdiction requires (default: 30 days).
- Have a process to answer data subject access requests.
- Have a process to answer takedown requests from site owners. A site owner asking you to stop scraping their site is a legal signal, not a suggestion.

**Step 6 — Publish a disclosure.**

- If you host nexus as a public service, publish a page that says what the service does, which categories of data it stores, and how to contact you.
- Link to this license file from that page.

**Step 7 — Review on a schedule.**

- Quarterly: check for new third-party license obligations after dependency updates.
- Annually: review your list of authorized targets.
- Whenever you add a new API client: read that API's terms.

---

## 7 · Frequently asked questions

**Can I use nexus in a commercial product?**
Yes. MIT permits commercial use. Clause A requires attribution. Clause C requires that your use complies with local law.

**Do I have to publish my modifications?**
No. MIT is not copyleft. You can keep your changes private. If you redistribute the Software, you must include this license file. If you host a service, you do not need to publish your source — unless you use PyMuPDF, which is AGPL, and then you do. See Section B.1.

**Do I have to credit the original author if I only copied one function?**
A general mention in your README is sufficient for a single function. For a larger set of copied code, follow Clause A.3.

**Can I strip the copyright header from Scraper.py?**
No. Clause A.1 requires preserving the header.

**Can I use nexus to scrape Twitter, Reddit, or YouTube?**
Only if you have authorization. Their terms prohibit scraping without an API agreement. Using impersonation to bypass their bot detection is a Clause C.2 violation. The project is not built to defeat their controls; do not repurpose it to.

**Can I remove the rate limiter?**
You may modify your deployment however you like. You may not use the modified deployment against sites you do not own without authorization. Removing the rate limiter is a legal risk you take, not a technical one.

**What if a site owner asks me to stop?**
Stop. A takedown notice is the most cost-effective legal signal you will receive. Ignoring it converts a business dispute into a legal one.

**Can I sell a hosted service built on nexus?**
Yes, provided you comply with Clause A (attribution), Clause C (authorized use), and Section B.1 (PyMuPDF AGPL), and that your users accept your own terms of service.

**Does this license cover the fonts?**
No. Fonts are licensed under SIL OFL by their respective authors. See Section B.2.

**Does this license cover the dependencies?**
No. Each dependency is licensed by its own authors. See Section B.1. Your use of nexus implies your acceptance of their terms.

**Is there a patent grant?**
No. The MIT license is silent on patents. If patent concerns are material to your use case, negotiate a separate agreement with the copyright holders.

**What happens if I violate this license?**
The license terminates automatically on breach, as stated in Section 1. You retain no rights to the Software, and any distribution or service you built on it becomes unlicensed.

**Is this document itself licensed?**
This document is licensed under CC BY 4.0. You may reuse and adapt it with attribution.

---

## 8 · Contact and disclosure

For licensing questions, contributions, or security disclosures:

- Open an issue in the project repository.
- For security disclosures, use the process described in `SECURITY.md`.
- For takedown requests, use the contact listed on the project's public site.

---

**End of license.**
**nexus v1.2.0 · October 2026**
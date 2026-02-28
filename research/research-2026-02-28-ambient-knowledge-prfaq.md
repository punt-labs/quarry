# Research: Quarry Ambient Knowledge PR/FAQ Claims

**Date:** 2026-02-28
**Request:** Verify eight claims for a PR/FAQ about "Quarry Ambient Knowledge" — a Claude Code plugin that passively learns from every coding session and actively recalls relevant knowledge.
**Claims investigated:** 8

---

## Evidence Found

---

**Claim 1**: Context loss between AI coding sessions is a real problem that developers report as a pain point.
**Verdict**: SUPPORTED

**Sources**:

- [Qodo "State of AI Code Quality" 2025 report](https://www.qodo.ai/reports/state-of-ai-code-quality/): Context pain increases with developer seniority — 41% among junior devs to 52% among senior devs report context-related frustration. 26% of all "top-3 improvement" votes focused on "improved contextual understanding," the single most-requested improvement (edging out "reduced hallucinations" at 24%). When context is persistently stored and reused across sessions, frustration drops from 54% (manual selection) to 16%.
- [Stack Overflow Developer Survey 2025](https://survey.stackoverflow.co/2025/ai): 84% of developers use AI tools, but 46% distrust AI accuracy — up from 31% in 2024. The biggest frustration (66% of respondents) is "AI solutions that are almost right, but not quite," which connects directly to context gaps. 87% are concerned about accuracy; 81% have security/privacy concerns about AI data use.
- [DEV Community — "The Hidden Cost of AI Coding Context Loss"](https://dev.to/gonewx/the-hidden-cost-of-ai-coding-context-loss-and-how-developers-are-fixing-it-4b0d): Documents the "goldfish memory" problem by name: each new chat session loses all previously established context about codebase patterns, architecture decisions, and prior debugging work. Describes team knowledge being "trapped in individual chat sessions." NOTE: This is a blog post/opinion piece, not primary survey data — treat as tertiary supporting evidence.
- [SageOx competitive analysis (local research)](./sageox-competitive-analysis.md): SageOx's own market framing describes the same problem: "decisions fragment," "PR origins lack context," "humans repeatedly explain intent," "architecture slowly drifts." A funded startup entering this market is corroborating evidence that the pain is real.

**Contradictory evidence**: The Stack Overflow survey shows AI adoption continuing to grow despite context frustrations, meaning developers are working around the problem rather than abandoning AI tools. Context loss may be a "tolerated friction" rather than a blocker. No survey directly quantifies hours lost to context re-establishment.
**Recommendation**: Use as-is, but cite the Qodo report's specific 52%/26% numbers rather than making the claim vaguely. The Qodo framing (context persistence reduces frustration from 54% to 16%) is the strongest single data point.

---

**Claim 2**: SageOx is a funded startup solving "shared team memory for AI agents."
**Verdict**: PARTIALLY SUPPORTED

**Sources**:

- [SageOx website (sageox.ai)](https://sageox.ai) via existing local research at [sageox-competitive-analysis.md](./sageox-competitive-analysis.md): The product exists and is accurately described — captures agent coding sessions as durable team artifacts, four-stage pipeline (Capture/Structure/Consult/Ship), integrates with Cursor/Copilot/Claude Code, has an open-source CLI (ox CLI on GitHub at sageox/ox).
- [GitHub — sageox/ox](https://github.com/sageox/ox): Open-source Go CLI confirmed by a real merged PR (sageox/ox#4) showing product links in PR bodies.

**What could not be verified**: Funding amount, lead investors, founding team names, and founding date are not indexed on Crunchbase, PitchBook, or in any press coverage found. The company has no public funding announcement. No founder names surfaced in any search.
**Contradictory evidence**: No contradictory evidence about the product's existence. The absence of funding data means the "funded" claim cannot be verified — the company may be bootstrapped, pre-announcement, or very early-stage.
**Recommendation**: Remove the "funded" qualifier unless you have a primary source (e.g., direct communication from the company, a press release, or a Crunchbase entry). Change to: "SageOx (sageox.ai), a startup building shared team memory for AI agents." The product's existence and positioning are verified; the funding status is not.

---

**Claim 3**: Entire.io was founded by former GitHub CEO Thomas Dohmke, raised $60M, and captures reasoning behind AI-generated code.
**Verdict**: SUPPORTED

**Sources**:

- [TechCrunch, February 10 2026](https://techcrunch.com/2026/02/10/former-github-ceo-raises-record-60m-dev-tool-seed-round-at-300m-valuation/): Confirms Thomas Dohmke (GitHub CEO 2021–August 2025) founded Entire and raised $60M seed round at $300M valuation. Led by Felicis; co-investors include Madrona, M12, Basis Set, Harry Stebbings, Jerry Yang, Datadog CEO Olivier Pomel, Y Combinator CEO Garry Tan. Described as the largest-ever seed round for a developer tools startup.
- [Entire.io official news](https://entire.io/news/former-github-ceo-thomas-dohmke-raises-60-million-seed-round): Confirms round details from primary source.
- [GitHub — entireio/cli](https://github.com/entireio/cli): Confirms the open-source Checkpoints product, which captures prompts, transcripts, agent decision steps, implementation logic, and token usage. Stores session data on a `entire/checkpoints/v1` git shadow branch. Integrates with Claude Code, Gemini CLI, OpenCode, and Cursor.
- [OSTechNix writeup on Entire CLI](https://ostechnix.com/entire-cli-git-observability-ai-agents/): Describes the "provenance gap" framing — git tells you *what* changed, but the reasoning evaporates once the AI's context window closes. Entire closes this gap.
- [Kosli — hands-on experiment with Entire](https://www.kosli.com/blog/governing_ai-generated_code_a_hands-on_experiment_with_entire_and_kosli/): Confirms the governance/auditability use case and that Checkpoints stores transcripts on the git shadow branch.

**Contradictory evidence**: None. All sources are consistent. The local research file at [vision.md](./vision.md) already incorporated accurate details about the shadow-branch architecture.
**Recommendation**: Use as-is. All three facts (Dohmke, $60M, reasoning capture) are strongly supported by multiple credible sources including primary announcement from Entire itself and TechCrunch reporting.

---

**Claim 4**: Claude Code has 115,000 developers (July 2025), doubling WAU between Jan–Feb 2026, $2.5B annualized revenue.
**Verdict**: PARTIALLY SUPPORTED — all three figures exist in public sources, but with important attribution caveats.

**Sources**:

- 115,000 developers / 195M lines per week (July 2025): [ppc.land summary](https://ppc.land/claude-code-reaches-115-000-developers-processes-195-million-lines-weekly/) — widely reported but originated from Deedy Das (Menlo Ventures VC), not from an official Anthropic announcement. Anthropic confirmed only a "5.5x increase in Claude Code revenue by July 2025." The 115,000 figure is a credible third-party disclosure, not a primary Anthropic stat.
- Doubling WAU Jan–Feb 2026 and $2.5B annualized revenue: [Anthropic official Series G announcement, February 12 2026](https://www.anthropic.com/news/anthropic-raises-30-billion-series-g-funding-380-billion-post-money-valuation) — confirmed by multiple outlets including [Yahoo Finance](https://finance.yahoo.com/news/anthropic-secures-30bn-series-g-072626603.html), [Gigazine](https://gigazine.net/gsc_news/en/20260213-anthropic-30-billion-series-g-funding/), and [Constellation Research](https://www.constellationr.com/insights/news/anthropics-claude-code-revenue-doubled-jan-1). These are sourced to the official Anthropic press release — strong primary source.
- Additional context: Claude Code reached $1B ARR in November 2025 (6 months post-GA). By February 2026 it had more than doubled to $2.5B. Business subscriptions quadrupled since January 1, 2026.

**Contradictory evidence**: The $130M annualized revenue estimate attributed to the same July 2025 Deedy Das disclosure (based on $1,000/dev/year assumption) is dramatically lower than the $2.5B figure from February 2026 — but these are different points in time, not contradictions. No sources dispute either figure.
**Recommendation**: Revise attribution. The 115,000 figure should be attributed to Deedy Das (Menlo Ventures) not to Anthropic directly. The doubling WAU and $2.5B revenue figures can be cited as official Anthropic disclosures tied to the Series G announcement.

---

**Claim 5**: Limitless was acquired by Meta in December 2025 and shut down its consumer product.
**Verdict**: SUPPORTED

**Sources**:

- [TechCrunch, December 5 2025](https://techcrunch.com/2025/12/05/meta-acquires-ai-device-startup-limitless/): Meta acquired Limitless (formerly Rewind). CEO Dan Siroker announced via corporate blog post. Financial terms not disclosed. Limitless had raised $33M+ from Sam Altman, First Round Capital, a16z, and NEA. Founded by Dan Siroker (co-founder/former CEO of Optimizely) and Brett Bejcek. Team (~50 employees) moves to Meta's Reality Labs wearables organization.
- [9to5Mac, December 5 2025](https://9to5mac.com/2025/12/05/rewind-limitless-meta-acquisition/): Rewind Mac app shutting down. Screen/audio capture disabled starting December 19, 2025. Device sales stopped December 5, 2025. Existing customers supported for one year but moved to Unlimited Plan at no cost.
- [WinBuzzer, December 5 2025](https://winbuzzer.com/2025/12/05/meta-acquires-ai-wearables-startup-limitless-kills-pendant-sales-and-sunsets-rewind-app-xcxwbn/): Pendant sales killed, Rewind app sunsetted. EU/UK users must export data before December 19 deletion.
- [mlq.ai](https://mlq.ai/news/meta-acquires-ai-wearables-startup-limitless-ending-sales-of-pendant-device/): Same details confirmed, notes Meta acquiring to accelerate AI-enabled wearables work.

**Contradictory evidence**: No contradictions. The acquisition is confirmed across multiple independent outlets. The Rewind desktop product was shut down (not just the pendant). Service maintained for existing customers for one year, but all new signups and sales stopped.
**Recommendation**: Use as-is. Clarify that the "consumer product" includes both the hardware pendant and the Rewind Mac app (desktop screen-recording product). The PR/FAQ claim is accurate but understates what was shut down — both products were affected.

---

**Claim 6**: The market for AI coding session memory / context persistence includes other tools beyond SageOx and Entire.io.
**Verdict**: SUPPORTED — the market is more crowded than the PR/FAQ implies.

**Sources**:

- [OneContext](https://x.com/JundeMorsenWu/status/2020161412593774922): Developer-built (Junde Wu) persistent context layer for Claude Code and Codex. Agent self-managed context across sessions, devices, and tools. Launched February 2026. Not a startup — currently a community/open-source project. Went viral quickly (landed Wu an interview at Google AI per social reports).
- [Windsurf (Codeium) — Cascade Memories](https://www.codeant.ai/blogs/best-ai-code-editor-cursor-vs-windsurf-vs-copilot): Windsurf has built-in "Cascade Memories" — cross-conversation persistent context for ongoing projects. This is a native feature of a major AI code editor, not a separate tool. The feature remembers context across sessions (e.g., remembered a port conflict from several conversations prior).
- [Letta (MemGPT)](https://www.tribe.ai/applied-ai/beyond-the-bubble-how-context-aware-memory-systems-are-changing-the-game-in-2025): Framework for stateful agents with persistent, editable memory blocks. Developer-focused but more of an agent framework than a coding-specific tool.
- [LangMem SDK](https://thenewstack.io/memory-for-ai-agents-a-new-paradigm-of-context-engineering/): Specialized memory toolkit for agent long-term memory. Part of LangChain ecosystem.
- [MemOS](https://www.tribe.ai/applied-ai/beyond-the-bubble-how-context-aware-memory-systems-are-changing-the-game-in-2025): Memory as an OS-level concern — coordinates different stores (facts, summaries, experiences).
- [Google ADK](https://vanducng.dev/2026/01/12/Google-Context-Engineering-Sessions-Memory-Summary/): Agent Development Kit with session management and state persistence primitives.
- [Arxiv: "Codified Context"](https://arxiv.org/html/2602.20478): Academic paper on infrastructure for AI agents in complex codebases — documents the context persistence problem at research level.

**Contradictory evidence**: Windsurf's Cascade Memories is a direct competitive feature in a well-funded major product (Codeium). This partially contradicts the framing that context persistence is an unsolved problem — at least one major player has built it natively. However, Windsurf's solution is cloud-hosted and Windsurf-specific; it does not work across tools or support local-first data.
**Recommendation**: Add Windsurf Cascade Memories to the competitive landscape. Update the PR/FAQ framing to acknowledge that context persistence is being worked on by multiple players, but position Quarry's differentiation as: (1) local-first/no cloud, (2) cross-tool (works with any agent), (3) semantic search over captured content, (4) open source.

---

**Claim 7**: Claude Code hooks/plugin ecosystem has significant adoption and developer interest.
**Verdict**: SUPPORTED

**Sources**:

- [StarupHub AI — "Anthropic's Claude Code plugins open the floodgates"](https://www.startuphub.ai/ai-news/ai-research/2025/anthropics-claude-code-plugins-open-the-floodgates/): Plugins launched in public beta October 9, 2025 with Claude Code version 1.0.33. The ecosystem grew from zero to 9,000+ plugins in under five months.
- [AI Tool Analysis — Claude Code Plugins Review 2026](https://aitoolanalysis.com/claude-code-plugins/): 9,000+ extensions as of early 2026.
- [Composio blog on Claude Code plugins](https://composio.dev/blog/claude-code-plugin): Details hook system, slash commands, subagents. Notes simplicity of directory-based architecture with no build step, no registry approval.
- [Morph — Claude Code Plugins guide](https://www.morphllm.com/claude-code-plugins): Confirms plugin marketplace accessible via `/plugin marketplace add`.
- [VentureBeat on Claude Cowork](https://venturebeat.com/orchestration/anthropic-says-claude-code-transformed-programming-now-claude-cowork-is): Epic's Seth Hain quoted: "over half of our use of Claude Code is by non-developer roles." Anthropic bundled Claude Code into Team/Enterprise plans August 2025.

**Contradictory evidence**: Community reports of "plugin fatigue" — too many plugins consume context window and degrade performance. Rapid model evolution means plugins can become obsolete weekly. These are real friction points but do not contradict ecosystem size.
**Recommendation**: Use as-is. The 9,000+ plugins figure is the key number. Add caveat: the hook system is powerful but context-window consumption is a real constraint that makes lightweight, fail-open hooks (like Quarry's design) preferable to heavyweight ones.

---

**Claim 8**: Developers prefer local-first tools for sensitive work (code, design docs, architecture decisions).
**Verdict**: PARTIALLY SUPPORTED — developer concern about data privacy is well documented; direct preference for "local-first" as an explicit choice is less directly measured.

**Sources**:

- [SonarSource State of Code Developer Survey 2026](https://www.sonarsource.com/state-of-code-developer-survey-report.pdf): 57% of developers worry about AI code exposing sensitive company or customer data. In enterprises with 1,000+ employees, this rises to 61%. 47% concerned about new security vulnerabilities from AI.
- [Stack Overflow 2025 Developer Survey](https://survey.stackoverflow.co/2025/): 81% have security/privacy concerns about AI data use. 75% would still want a human for help because they don't trust AI answers. 61.7% cite ethical/security concerns about code.
- [Kinetools — "Privacy-First Development Tools: Why Local Processing Matters"](https://kinetools.com/blog/privacy-first-development-tools): Blog post documenting the preference for local processing tools among privacy-conscious developers. Processing locally = no compliance burden. However this is a blog post promoting local tools, not primary survey data.
- [SonarSource report on shadow AI](https://www.sonarsource.com/state-of-code-developer-survey-report.pdf): Shadow AI use of personal accounts creates security blind spots. Cursor shows only 27% personal account use vs. 64% work-sanctioned — suggesting formal/top-down tool selection prioritizes control.

**Contradictory evidence**: The data shows developer *concern* about privacy, but not necessarily a *revealed preference* for local-first tools over cloud tools that are company-sanctioned. Many developers use cloud-hosted AI tools even with these concerns (84% adoption despite 81% with privacy concerns). The SonarSource preference metrics are from a vendor with a stake in the conversation.
**Recommendation**: Revise to "developers are significantly concerned about sensitive data exposure through cloud-based AI tools" rather than "prefer local-first." The concern is well-documented; the preference is inferred. Cite the 57% (SonarSource) and 81% (Stack Overflow) figures specifically. Local-first as an architecture choice addresses these documented concerns.

---

## Bibliography Entries

```bibtex
@report{qodo2025aicodequality,
  author       = {{Qodo}},
  title        = {State of AI Code Quality in 2025},
  year         = {2025},
  url          = {https://www.qodo.ai/reports/state-of-ai-code-quality/},
  institution  = {Qodo},
  note         = {Developer survey: context pain affects 41-52% of developers by seniority; "improved contextual understanding" is the top-requested AI improvement (26% of votes); persistent context reduces frustration from 54% to 16%.},
}

@report{stackoverflow2025survey,
  author       = {{Stack Overflow}},
  title        = {2025 Stack Overflow Developer Survey},
  year         = {2025},
  url          = {https://survey.stackoverflow.co/2025/},
  institution  = {Stack Overflow},
  note         = {49,000+ respondents. 84% use AI tools; 46% distrust accuracy (up from 31%); 66% cite "almost right but not quite" as top frustration; 81% have security/privacy concerns about AI data use; 87% concerned about accuracy.},
}

@online{stackoverflow2025blog,
  author       = {{Stack Overflow}},
  title        = {Developers remain willing but reluctant to use AI: The 2025 Developer Survey results are here},
  year         = {2025},
  url          = {https://stackoverflow.blog/2025/12/29/developers-remain-willing-but-reluctant-to-use-ai-the-2025-developer-survey-results-are-here/},
  note         = {Official Stack Overflow blog summary of 2025 survey results, including AI trust decline findings.},
}

@online{techcrunch2026entirefunding,
  author       = {{TechCrunch}},
  title        = {Former GitHub CEO raises record \$60M dev tool seed round at \$300M valuation},
  year         = {2026},
  url          = {https://techcrunch.com/2026/02/10/former-github-ceo-raises-record-60m-dev-tool-seed-round-at-300m-valuation/},
  note         = {Confirms Thomas Dohmke (GitHub CEO 2021-2025) founded Entire; \$60M seed at \$300M valuation led by Felicis; largest-ever developer tools seed round. Product captures prompts, transcripts, and reasoning behind AI-generated code.},
}

@online{entire2026announcement,
  author       = {{Entire}},
  title        = {Former GitHub CEO Thomas Dohmke Raises \$60 Million Seed Round},
  year         = {2026},
  url          = {https://entire.io/news/former-github-ceo-thomas-dohmke-raises-60-million-seed-round},
  note         = {Primary announcement from Entire.io confirming \$60M seed round and product launch of Checkpoints CLI.},
}

@online{entireio2026github,
  author       = {{Entire}},
  title        = {entireio/cli: Entire is a new developer platform that hooks into your git workflow to capture AI agent sessions on every push},
  year         = {2026},
  url          = {https://github.com/entireio/cli},
  note         = {Open-source Checkpoints CLI. Captures prompts, transcripts, tool calls, and token usage. Stores session data on entire/checkpoints/v1 git shadow branch. Supports Claude Code, Gemini CLI, OpenCode, Cursor.},
}

@online{anthropic2026seriesg,
  author       = {{Anthropic}},
  title        = {Anthropic raises \$30 billion in Series G funding at \$380 billion post-money valuation},
  year         = {2026},
  url          = {https://www.anthropic.com/news/anthropic-raises-30-billion-series-g-funding-380-billion-post-money-valuation},
  note         = {Primary Anthropic source: Claude Code WAU doubled since January 1 2026; annualized revenue exceeded \$2.5B; total Anthropic ARR \$14B. Announced February 12 2026.},
}

@online{ppcland2025claudecode115k,
  author       = {{ppc.land}},
  title        = {Claude Code reaches 115,000 developers, processes 195 million lines weekly},
  year         = {2025},
  url          = {https://ppc.land/claude-code-reaches-115-000-developers-processes-195-million-lines-weekly/},
  note         = {Reports figures disclosed by Deedy Das (Menlo Ventures) on July 6 2025 — NOT an official Anthropic release. 115,000 developers, 195M lines per week. Original Anthropic-confirmed stat: 5.5x revenue increase by July 2025.},
}

@online{techcrunch2025limitless,
  author       = {{TechCrunch}},
  title        = {Meta acquires AI device startup Limitless},
  year         = {2025},
  url          = {https://techcrunch.com/2025/12/05/meta-acquires-ai-device-startup-limitless/},
  note         = {Meta acquired Limitless (formerly Rewind) December 5 2025. Founded by Dan Siroker and Brett Bejcek. Raised \$33M+ from Sam Altman, First Round, a16z, NEA. ~50 employees join Meta Reality Labs wearables team. Financial terms undisclosed.},
}

@online{9to5mac2025rewindshutdown,
  author       = {{9to5Mac}},
  title        = {Rewind Mac app shutting down following Meta acquisition},
  year         = {2025},
  url          = {https://9to5mac.com/2025/12/05/rewind-limitless-meta-acquisition/},
  note         = {Confirms Rewind Mac app (desktop screen-and-audio recorder) shutdown effective December 19 2025. Pendant sales stopped December 5. EU/UK user data deleted after December 19.},
}

@online{winbuzzer2025limitlessshutdown,
  author       = {{WinBuzzer}},
  title        = {Meta Acquires AI Wearables Startup Limitless, Kills "Pendant" Sales and Sunsets Rewind App},
  year         = {2025},
  url          = {https://winbuzzer.com/2025/12/05/meta-acquires-ai-wearables-startup-limitless-kills-pendant-sales-and-sunsets-rewind-app-xcxwbn/},
  note         = {Corroborates pendant sale termination and Rewind app sunset on December 5 2025.},
}

@online{startuphub2025claudeplugins,
  author       = {{StartupHub AI}},
  title        = {Anthropic's Claude Code plugins open the floodgates},
  year         = {2025},
  url          = {https://www.startuphub.ai/ai-news/ai-research/2025/anthropics-claude-code-plugins-open-the-floodgates/},
  note         = {Claude Code plugins launched public beta October 9 2025. Ecosystem grew to 9,000+ plugins in under five months. Describes hook system and directory-based architecture.},
}

@online{aitoolanalysis2026claudeplugins,
  author       = {{AI Tool Analysis}},
  title        = {Claude Code Plugins Review 2026: 9,000+ Extensions},
  year         = {2026},
  url          = {https://aitoolanalysis.com/claude-code-plugins/},
  note         = {Reports 9,000+ Claude Code plugins available as of early 2026. Covers plugin categories and marketplace structure.},
}

@report{sonar2026stateofcode,
  author       = {{SonarSource}},
  title        = {State of Code Developer Survey Report 2026},
  year         = {2026},
  url          = {https://www.sonarsource.com/state-of-code-developer-survey-report.pdf},
  institution  = {SonarSource},
  note         = {57% of developers worry about AI code exposing sensitive company or customer data. Rises to 61% in enterprises with 1,000+ employees. 47% concerned about AI-introduced security vulnerabilities.},
}

@online{kinetools2025localfirst,
  author       = {{Kinetools}},
  title        = {Privacy-First Development Tools: Why Local Processing Matters},
  year         = {2025},
  url          = {https://kinetools.com/blog/privacy-first-development-tools},
  note         = {TERTIARY SOURCE — blog post by local-tool vendor. Documents developer preference for local processing: no server upload, no accounts, no tracking. Local compliance advantage. Use with caution given vendor bias.},
}

@online{onecontext2026,
  author       = {Wu, Junde},
  title        = {Introducing OneContext (X/Twitter post)},
  year         = {2026},
  url          = {https://x.com/JundeMorsenWu/status/2020161412593774922},
  note         = {OneContext: self-managed persistent context layer for Claude Code and Codex, launched February 2026. Cross-session, cross-device, cross-agent. Open-source community project, not a funded startup.},
}

@online{thenewstack2026entiredohmke,
  author       = {{The New Stack}},
  title        = {GitHub's former CEO launches a developer platform for the age of agentic coding},
  year         = {2026},
  url          = {https://thenewstack.io/thomas-dohmke-interview-entire/},
  note         = {Interview with Thomas Dohmke. Confirms GitHub CEO from 2021 to August 2025. Dohmke's vision: "specifications, reasoning, session logs, intent, outcomes" as the new developer platform layer. Plans open-source playbook with hosted service.},
}

@online{codeant2025windsurf,
  author       = {{CodeAnt AI}},
  title        = {AI Code Editors Showdown 2025: Cursor vs Windsurf vs Copilot Explained},
  year         = {2025},
  url          = {https://www.codeant.ai/blogs/best-ai-code-editor-cursor-vs-windsurf-vs-copilot},
  note         = {Documents Windsurf Cascade Memories feature: persistent cross-conversation context, remembers project details across sessions. Direct competitive feature to Quarry ambient recall.},
}

@online{tribe2025agentmemory,
  author       = {{Tribe AI}},
  title        = {Beyond the Bubble: How Context-Aware Memory Systems Are Changing the Game in 2025},
  year         = {2025},
  url          = {https://www.tribe.ai/applied-ai/beyond-the-bubble-how-context-aware-memory-systems-are-changing-the-game-in-2025},
  note         = {Overview of agent memory landscape. Covers Letta/MemGPT, MemOS, LangMem, and other frameworks. Frames sessions (working memory) vs. memory (long-term cross-session persistence) as the core architectural distinction.},
}
```

---

## Research Gaps

**Claim**: SageOx is a funded startup (specific claim of funding status).
**What's missing**: No public funding announcement, Crunchbase entry, or press coverage of a financing round for SageOx/sageox.ai exists in any indexed source. The company's ox CLI is open source on GitHub but the founding team, investor names, and funding amount (if any) are not publicly documented.
**Suggested action**: Contact SageOx directly or check their website for a team/about page. The product claim ("shared team memory for AI agents") is verified; "funded" cannot be stated without a source.

---

**Claim**: Claude Code adoption numbers — 115,000 developers (July 2025) is a confirmed Anthropic stat.
**What's missing**: This figure originated from a VC (Deedy Das at Menlo Ventures), not an official Anthropic release. Anthropic confirmed only "5.5x revenue increase by July 2025" officially. The 115K number is widely cited but not traceable to an Anthropic primary source.
**Suggested action**: Either attribute explicitly to Deedy Das/Menlo Ventures, or replace with the Anthropic-confirmed "$1B ARR by November 2025" milestone which has a clear primary source.

---

**Claim**: Specific developer hours lost per week to context re-establishment.
**What's missing**: No survey quantifies hours-per-week lost specifically to AI context loss between sessions. The Qodo survey provides percentage-of-developers-affected but not time cost.
**Suggested action**: Accept as an assumption in the PR/FAQ or conduct targeted interviews with developers to get first-person time estimates.

---

**Claim**: Zed code editor's context persistence features.
**What's missing**: No evidence found that Zed has cross-session context persistence comparable to Windsurf Cascade Memories. Zed is a high-performance editor with growing AI features, but no memory/context-persistence feature was documented in search results.
**Suggested action**: Check Zed's official changelog or AI features documentation directly. This gap means Zed should not be cited as a context-persistence competitor without further verification.

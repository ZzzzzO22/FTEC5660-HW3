#!/usr/bin/env python3
"""FTEC5660 HW3 student starter: an agent that applies 3 of Fisher's 15 points using tools."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sqlite3
import statistics
import threading
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


MODEL_NAME = "deepseek-v4-flash"
INTERVIEW_LIMIT = 10
MAX_SQL_ROWS = 50

# Philip Fisher, "Common Stocks and Uncommon Profits", Part One, Chapter 3.
# For every point, PASS means the company looks good on it. You choose 3
# points and decide yourself what PASS means for each of them.
FISHER_POINTS = {
    1: "Does the company have products or services with sufficient market potential to make possible a sizable increase in sales for at least several years?",
    2: "Does the management have a determination to continue to develop products or processes that will still further increase total sales potentials when the growth potentials of currently attractive product lines have largely been exploited?",
    3: "How effective are the company's research and development efforts in relation to its size?",
    4: "Does the company have an above-average sales organization?",
    5: "Does the company have a worthwhile profit margin?",
    6: "What is the company doing to maintain or improve profit margins?",
    7: "Does the company have outstanding labor and personnel relations?",
    8: "Does the company have outstanding executive relations?",
    9: "Does the company have depth to its management?",
    10: "How good are the company's cost analysis and accounting controls?",
    11: "Are there other aspects of the business, somewhat peculiar to the industry involved, which will give the investor important clues as to how outstanding the company may be in relation to its competition?",
    12: "Does the company have a short-range or long-range outlook in regard to profits?",
    13: "In the foreseeable future will the growth of the company require sufficient equity financing so that the larger number of shares then outstanding will largely cancel the existing stockholders' benefit from this anticipated growth?",
    14: "Does the management talk freely to investors about its affairs when things are going well but 'clam up' when troubles and disappointments occur?",
    15: "Does the company have a management of unquestionable integrity?",
}
VERDICTS = ("PASS", "FAIL")


def load_env_file(path: Path = Path(".env")) -> None:
    """Load the simple KEY=VALUE entries used by this homework."""
    if not path.is_file():
        return
    import os

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


# Choose any 3 of the 15 points, e.g. CHOSEN_POINTS = [1, 7, 15]. Your agent
# gives a PASS or FAIL verdict on these 3 points only, with your own rule for each.
CHOSEN_POINTS: list[int] = [1, 5, 4]

GROWTH_POINT, MARGIN_POINT, SALES_POINT = 1, 5, 4
FULL_YEAR_MONTHS = 12
# 14 of the 20 public companies pass any single point, so PASS is the safer
# guess when the evidence never arrives.
DEFAULT_VERDICT = "PASS"
DEFAULT_REASON = "the evidence did not arrive"
COMPANY_TIMEOUT_S = 200.0
MAX_COMPANIES_AT_ONCE = 6
JUDGE_MAX_TOKENS = 6000
WRITER_MAX_TOKENS = 900

QUERY_HEADER = """
SELECT c.industry AS industry,
       m.revenue_growth_2y_pct AS growth_median,
       m.operating_margin_pct AS margin_median
FROM company c LEFT JOIN industry_medians m ON m.industry = c.industry
"""

QUERY_REVENUE = """
SELECT substr(order_date, 1, 4) AS year, SUM(amount) AS revenue
FROM orders GROUP BY year
"""

QUERY_COSTS = """
SELECT substr(month, 1, 4) AS year, COUNT(*) AS months,
       SUM(cost_of_sales) AS cost_of_sales,
       SUM(selling_admin) AS selling_admin,
       SUM(research_development) AS research
FROM monthly_costs GROUP BY year
"""

QUERY_MEDIANS = """
SELECT revenue_growth_2y_pct AS growth_median, operating_margin_pct AS margin_median
FROM industry_medians
"""

SALES_RUBRIC = (
    "You are applying one point of Philip Fisher's checklist: does the company "
    "have an above-average sales organisation?\n\n"
    "Two independent witnesses described how the company sells. Judge the way it "
    "sells, not what it makes.\n\n"
    "PASS looks like: the buyer says the sales staff know the account, visit, or "
    "turn up with technical help when something goes wrong; the rival admits the "
    "force is strong, better trained, or feared in competitive deals.\n"
    "FAIL looks like: the buyer serves themselves, gets a price list and little "
    "else, or goes elsewhere for advice; the rival says the force is weak, "
    "absent, or not a factor in the deals they compete for.\n\n"
    "A witness who cannot comment tells you nothing, so ignore that line.\n"
    "Ignore remarks about price, delivery or product quality unless they bear on "
    "how the company sells.\n\n"
    "Answer with one JSON object and nothing else:\n"
    '{"verdict": "PASS" or "FAIL", "reason": "one sentence citing the witnesses"}'
)

VERIFY_RUBRIC = (
    "You are checking a colleague's verdict on Fisher's sales-organisation point "
    "against the witness statements. Say whether the verdict follows from them. "
    "Do not move your standard just to agree with the colleague.\n"
    'Answer with JSON only: {"agree": true or false, "reason": "one sentence"}'
)

SUMMARY_RUBRIC = (
    "Write two or three sentences, well under seventy words, for an investor "
    "explaining three verdicts on Fisher's checklist. Give the reason for each "
    "verdict, with the figure or the quote that carries it. Use only the figures "
    "given under Facts: do not compute new ones and do not add outside knowledge. "
    "Quote at most six words of a witness statement. A side note is background, "
    "never the reason for a point. Do not start by announcing how many verdicts "
    "there are. Plain English, no bullet points, no preamble, no heading."
)


class ToolFailure(RuntimeError):
    """A tool answered in a way we cannot use."""


@dataclass
class Facts:
    """Everything the database can tell us about one company, already computed."""

    industry: str = ""
    growth_median: float | None = None
    margin_median: float | None = None
    revenue: dict[int, float] = field(default_factory=dict)
    cost: dict[int, dict[str, float]] = field(default_factory=dict)
    latest_year: int | None = None
    base_year: int | None = None
    growth_pct: float | None = None
    margin_pct: float | None = None
    research_cut_years: list[int] = field(default_factory=list)


def _tool(tools: list[Any], name: str) -> Any:
    for candidate in tools:
        if getattr(candidate, "name", None) == name:
            return candidate
    raise ToolFailure(f"the {name} tool is missing")


def _invoke(tools: list[Any], name: str, **arguments: Any) -> str:
    return str(_tool(tools, name).invoke(arguments))


def _rows(text: str) -> list[dict[str, str]]:
    """Split the sql_query tool output (header, then pipe-separated rows)."""
    lines = [line for line in text.strip().splitlines() if line.strip()]
    if not lines or "|" not in lines[0]:
        return []
    header = [cell.strip() for cell in lines[0].split("|")]
    rows: list[dict[str, str]] = []
    for line in lines[1:]:
        if line.startswith("..."):
            break
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) == len(header):
            rows.append(dict(zip(header, cells)))
    return rows


def _number(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _collect(tools: list[Any]) -> Facts:
    """Three aggregate queries, then all arithmetic in Python."""
    facts = Facts()

    header = _rows(_invoke(tools, "sql_query", query=QUERY_HEADER))
    if header:
        facts.industry = (header[0].get("industry") or "").strip()
        facts.growth_median = _number(header[0].get("growth_median"))
        facts.margin_median = _number(header[0].get("margin_median"))

    for row in _rows(_invoke(tools, "sql_query", query=QUERY_REVENUE)):
        year, revenue = _integer(row.get("year")), _number(row.get("revenue"))
        if year is not None and revenue is not None:
            facts.revenue[year] = revenue

    months: dict[int, int] = {}
    for row in _rows(_invoke(tools, "sql_query", query=QUERY_COSTS)):
        year = _integer(row.get("year"))
        if year is None:
            continue
        months[year] = _integer(row.get("months")) or 0
        facts.cost[year] = {
            "cost_of_sales": _number(row.get("cost_of_sales")) or 0.0,
            "selling_admin": _number(row.get("selling_admin")) or 0.0,
            "research": _number(row.get("research")) or 0.0,
        }

    # Only a year with twelve cost months and some orders counts as complete:
    # the newest year in the data stops in June.
    full_years = sorted(
        year for year, count in months.items()
        if count == FULL_YEAR_MONTHS and facts.revenue.get(year)
    )
    if full_years:
        facts.latest_year = full_years[-1]
        facts.base_year = full_years[-3] if len(full_years) >= 3 else full_years[0]
        base = facts.revenue.get(facts.base_year) or 0.0
        if base:
            facts.growth_pct = (facts.revenue[facts.latest_year] / base - 1) * 100
        latest = facts.revenue[facts.latest_year]
        cost = facts.cost[facts.latest_year]
        if latest:
            operating = latest - cost["cost_of_sales"] - cost["selling_admin"] - cost["research"]
            facts.margin_pct = operating / latest * 100
        facts.research_cut_years = [
            year for position, year in enumerate(full_years)
            if position and facts.cost[year]["research"] < facts.cost[full_years[position - 1]]["research"]
        ]

    # A company whose industry has no row in industry_medians still needs a line.
    if facts.growth_median is None or facts.margin_median is None:
        medians = _rows(_invoke(tools, "sql_query", query=QUERY_MEDIANS))
        growths = [v for v in (_number(r.get("growth_median")) for r in medians) if v is not None]
        margins = [v for v in (_number(r.get("margin_median")) for r in medians) if v is not None]
        if facts.growth_median is None and growths:
            facts.growth_median = statistics.median(growths)
        if facts.margin_median is None and margins:
            facts.margin_median = statistics.median(margins)

    return facts


def _decide_growth(facts: Facts) -> str:
    """Point 1: the company must outgrow its own industry, not some absolute rate."""
    if facts.growth_pct is None or facts.growth_median is None:
        return DEFAULT_VERDICT
    return "PASS" if facts.growth_pct >= facts.growth_median else "FAIL"


def _decide_margin(facts: Facts) -> str:
    """Point 5: worthwhile means above the median of the industry it competes in."""
    if facts.margin_pct is None or facts.margin_median is None:
        return DEFAULT_VERDICT
    return "PASS" if facts.margin_pct >= facts.margin_median else "FAIL"


def _ask_witnesses(tools: list[Any]) -> dict[str, str]:
    """Two interviews, both on the sales team; a witness who cannot speak is dropped."""
    witnesses: dict[str, str] = {}
    for role in ("customer", "competitor"):
        try:
            answer = _invoke(tools, "interview", role=role, topic="sales_team").strip()
        except Exception:  # noqa: BLE001 - one bad answer must not stop the company
            continue
        lowered = answer.lower()
        if "comment" in lowered and ("cannot" in lowered or "can't" in lowered):
            continue
        if "limit reached" in lowered or "unknown role" in lowered:
            continue
        witnesses[role] = answer
    return witnesses


def _json_object(text: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return {}
    try:
        value = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


NEGATIVE_SALES = (
    "just send", "price list", "only quotes", "is weak", "weak", "not a factor",
    "commodity", "cheapest", "whoever is cheaper", "go to rivals", "elsewhere for advice",
)
POSITIVE_SALES = (
    "knows our line", "knows our business", "better trained", "we fear", "win most",
    "next day with an engineer", "visits every", "account manager visits",
    "solves", "takes our calls", "we trust",
)


def _sales_from_text(text: str) -> str:
    """Deterministic fallback used only when the model cannot answer."""
    lowered = text.lower()
    score = sum(1 for phrase in POSITIVE_SALES if phrase in lowered)
    score -= sum(1 for phrase in NEGATIVE_SALES if phrase in lowered)
    if score > 0:
        return "PASS"
    if score < 0:
        return "FAIL"
    return DEFAULT_VERDICT


def _ask_model(model: Any, prompt: str) -> str:
    """One model call, retried once.

    This model thinks before it answers and the thinking is billed to max_tokens,
    so a long chain of thought comes back with finish_reason "length" and an empty
    string. An empty reply is not an answer, so ask again.
    """
    for _ in range(2):
        try:
            reply = model.invoke(prompt)
        except Exception:  # noqa: BLE001
            return ""
        text = str(getattr(reply, "content", reply) or "")
        if text.strip():
            return text
    return ""


def _sales_prompt(facts: Facts, witnesses: dict[str, str]) -> str:
    lines = [SALES_RUBRIC, "", f"Industry: {facts.industry or 'unknown'}"]
    lines += [f"{role.capitalize()}: {quote}" for role, quote in witnesses.items()]
    return "\n".join(lines)


def _judge_sales(agent: Any, facts: Facts, witnesses: dict[str, str]) -> tuple[str, str, str]:
    """The only point the model decides: read two quotes, call it."""
    if not witnesses:
        return DEFAULT_VERDICT, DEFAULT_REASON, "no witness"
    judge = (agent or {}).get("judge")
    if judge is not None:
        data = _json_object(_ask_model(judge, _sales_prompt(facts, witnesses)))
        verdict = str(data.get("verdict", "")).upper()
        if verdict in VERDICTS:
            return verdict, str(data.get("reason", "")).strip()[:300], "model"
    return (_sales_from_text(" ".join(witnesses.values())),
            "read from the wording of the two statements", "lexicon")


def _verify_sales(
    agent: Any, facts: Facts, witnesses: dict[str, str], verdict: str, reason: str
) -> tuple[str, str, bool]:
    """Reflection: a second pass whose only job is to disagree if it can."""
    judge = (agent or {}).get("judge")
    if judge is None or not witnesses:
        return verdict, reason, False
    lines = [VERIFY_RUBRIC, "", f"Verdict given: {verdict}"]
    lines += [f"{role.capitalize()}: {quote}" for role, quote in witnesses.items()]
    data = _json_object(_ask_model(judge, "\n".join(lines)))
    if data.get("agree") is not False:
        return verdict, reason, False
    objection = str(data.get("reason", "")).strip()[:300]
    lines = [SALES_RUBRIC, "", f"Industry: {facts.industry or 'unknown'}",
             f"A reviewer objected to the verdict {verdict}: {objection}", ""]
    lines += [f"{role.capitalize()}: {quote}" for role, quote in witnesses.items()]
    revised = str(_json_object(_ask_model(judge, "\n".join(lines))).get("verdict", "")).upper()
    if revised in VERDICTS:
        return revised, f"revised after review: {objection}"[:300], True
    return verdict, reason, True


def _claimed_growth(tools: list[Any]) -> float | None:
    """Read the annual letter and pull out the two-year growth it advertises."""
    try:
        listing = _invoke(tools, "list_documents")
    except Exception:  # noqa: BLE001
        return None
    for line in listing.splitlines():
        name = line.split(":", 1)[0].strip()
        if not name.startswith("annual_letter"):
            continue
        try:
            text = _invoke(tools, "read_document", name=name)
        except Exception:  # noqa: BLE001
            return None
        match = re.search(r"([\d.]+)%\s+more than in (20\d\d)", text)
        if match:
            return float(match.group(1))
    return None


def _fact_lines(facts: Facts, verdicts: dict[int, str], witnesses: dict[str, str],
                claimed: float | None) -> str:
    lines = []
    if facts.growth_pct is not None and facts.growth_median is not None:
        lines.append(
            f"Revenue {facts.base_year} to {facts.latest_year}: {facts.growth_pct:.1f}% growth; "
            f"industry median {facts.growth_median:.1f}%. Point 1 verdict: {verdicts[GROWTH_POINT]}.")
    if facts.margin_pct is not None and facts.margin_median is not None:
        lines.append(
            f"Operating margin {facts.latest_year}: {facts.margin_pct:.1f}%; "
            f"industry median {facts.margin_median:.1f}%. Point 5 verdict: {verdicts[MARGIN_POINT]}.")
    if claimed is not None:
        lines.append(f"The annual letter claims {claimed:.0f}% two-year revenue growth.")
    if facts.research_cut_years:
        years = ", ".join(str(y) for y in facts.research_cut_years)
        lines.append(
            f"Side note, background only and not the reason for any point: research "
            f"spending fell in {years}, though the letter says it never was cut.")
    for role, quote in witnesses.items():
        lines.append(f"{role.capitalize()} said: {quote}")
    lines.append(f"Point 4 verdict: {verdicts[SALES_POINT]}.")
    return "\n".join(lines)


def _tidy(text: str) -> str:
    """Single line, no wrapping quotes, and never cut in the middle of a sentence."""
    text = re.sub(r"\s+", " ", str(text)).strip()
    if len(text) > 1 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1].strip()
    if len(text) > 620:
        cut = max(text.rfind(". "), text.rfind("; "), text.rfind(", "))
        text = text[: cut + 1] if cut > 200 else text[:620]
    return text.strip()


def _grounded(text: str, facts_text: str) -> bool:
    """Every figure in the summary must already appear in the fact sheet."""
    allowed = set(re.findall(r"\d+(?:\.\d+)?", facts_text))
    for token in re.findall(r"\d+(?:\.\d+)?", text):
        if token in allowed:
            continue
        if re.fullmatch(r"20\d\d", token):
            continue
        if any(token in value or value in token for value in allowed):
            continue
        return False
    return True


def _fallback_summary(facts: Facts, verdicts: dict[int, str], witnesses: dict[str, str]) -> str:
    parts = []
    if facts.growth_pct is not None and facts.growth_median is not None:
        parts.append(
            f"Revenue grew {facts.growth_pct:.1f}% between {facts.base_year} and {facts.latest_year}, "
            f"against an industry median of {facts.growth_median:.1f}%, so point 1 is "
            f"{verdicts[GROWTH_POINT]}.")
    if facts.margin_pct is not None and facts.margin_median is not None:
        parts.append(
            f"The operating margin of {facts.margin_pct:.1f}% compares with {facts.margin_median:.1f}% "
            f"for the industry, so point 5 is {verdicts[MARGIN_POINT]}.")
    if witnesses:
        parts.append(f"Witnesses describe the sales force in terms that make point 4 "
                     f"{verdicts[SALES_POINT]}.")
    return _tidy(" ".join(parts) or "No evidence could be collected for this company.")


def _write_summary(agent: Any, facts: Facts, verdicts: dict[int, str],
                   witnesses: dict[str, str], claimed: float | None) -> str:
    facts_text = _fact_lines(facts, verdicts, witnesses, claimed)
    writer = (agent or {}).get("writer")
    if writer is not None:
        prompt = f"{SUMMARY_RUBRIC}\n\nFacts:\n{facts_text}"
        for _ in range(2):
            text = _tidy(_ask_model(writer, prompt))
            if text and _grounded(text, facts_text):
                return text
    return _fallback_summary(facts, verdicts, witnesses)


def _analyse(agent: Any, tools: list[Any]) -> dict[str, Any]:
    """One company, start to finish. Runs in a worker thread."""
    facts = _collect(tools)
    verdicts = {
        GROWTH_POINT: _decide_growth(facts),
        MARGIN_POINT: _decide_margin(facts),
    }
    witnesses = _ask_witnesses(tools)
    verdict, reason, source = _judge_sales(agent, facts, witnesses)
    verdict, reason, challenged = _verify_sales(agent, facts, witnesses, verdict, reason)
    verdicts[SALES_POINT] = verdict
    claimed = _claimed_growth(tools)
    summary = _write_summary(agent, facts, verdicts, witnesses, claimed)
    return {"points": verdicts, "summary": summary,
            "trace": {"reason": reason, "challenged": challenged, "source": source,
                      "witnesses": len(witnesses), "industry": facts.industry}}



def build_agent() -> Any:
    """Create and return your model or agent once.

    Suggested imports:
        from langchain_deepseek import ChatDeepSeek
        from langchain.agents import create_agent

    Use the DeepSeek model named by ``MODEL_NAME``. The API key is loaded
    from .env. Each company has its own tools (see ``analyze_companies``), so
    you will probably create one tool-calling agent per company there.
    """
    ### YOUR CODE HERE
    try:
        from langchain_deepseek import ChatDeepSeek
    except ImportError:
        return None
    try:
        return {
            # Decides point 4 and reviews that decision.
            "judge": ChatDeepSeek(model=MODEL_NAME, temperature=0, max_tokens=JUDGE_MAX_TOKENS),
            # Only writes prose; thinking off keeps the summary step cheap.
            "writer": ChatDeepSeek(
                model=MODEL_NAME, temperature=0.3, max_tokens=WRITER_MAX_TOKENS,
                extra_body={"thinking": {"type": "disabled"}},
            ),
        }
    except Exception:  # noqa: BLE001 - a missing key must still produce a results.csv
        return None


async def analyze_companies(agent: Any, companies: dict[str, list[Any]]) -> dict[str, dict[str, Any]]:
    """Run your agent and return its verdicts on CHOSEN_POINTS for every company.

    ``companies`` maps each company folder name to its four LangChain tools:

        list_documents()             names and dates of the company's documents
        read_document(name)          the text of one document
        sql_query(query)             one read-only SQLite query on company.db
        interview(role, topic)       one interview answer (INTERVIEW_LIMIT per company)

    Give the tools to a tool-calling agent, e.g.
    ``create_agent(model, tools=companies[name], system_prompt=...)``, or call
    them yourself with ``tool.invoke({...})``. The README explains the
    documents, the database tables and the interview roles and topics.

    Return one dictionary per company folder name, e.g. with
    CHOSEN_POINTS = [1, 7, 15]:

        {
            "hollins_group": {
                "points": {1: "PASS", 7: "FAIL", 15: "PASS"},
                "summary": "Two or three sentences explaining the verdicts.",
            },
            ...
        }

    Each chosen point needs a verdict ("PASS" or "FAIL"; True/False also
    works). Other points are ignored.

    Catch errors per company (e.g. a failed API call) and still return an
    entry for it: an exception here means no results.csv, which scores zero.
    You may analyse companies in parallel (e.g. ``asyncio.gather``).
    """
    ### YOUR CODE HERE
    semaphore = asyncio.Semaphore(MAX_COMPANIES_AT_ONCE)

    async def analyse_one(name: str, tools: list[Any]) -> tuple[str, dict[str, Any]]:
        async with semaphore:
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(_analyse, agent, tools), timeout=COMPANY_TIMEOUT_S
                )
                return name, result
            except Exception as exc:  # noqa: BLE001 - one company must not sink the run
                return name, {
                    "points": {point: DEFAULT_VERDICT for point in CHOSEN_POINTS},
                    "summary": f"Analysis failed ({type(exc).__name__}); verdicts defaulted.",
                    "trace": {"error": type(exc).__name__},
                }

    pairs = await asyncio.gather(*(analyse_one(name, tools) for name, tools in companies.items()))
    return dict(pairs)


# Everything below is provided runner/scoring code. No edits are needed.
# Your code must read company data only through these tools: do not open
# company.db or interviews.dat directly.

INTERVIEWS_USED: dict[str, int] = {}


def company_folders(folder: Path) -> list[Path]:
    """Return one sub-folder per company directly inside *folder*, sorted by name."""
    return sorted(p for p in folder.iterdir() if p.is_dir() and not p.name.startswith("."))


def make_tools(company_folder: Path) -> list[Any]:
    """Build the four tools for one company."""
    from langchain_core.tools import tool

    name = company_folder.name
    db_uri = f"{(company_folder / 'company.db').resolve().as_uri()}?mode=ro"
    answers = json.loads(zlib.decompress((company_folder / "interviews.dat").read_bytes()))
    documents = {p.name: p for p in sorted(company_folder.glob("*.md"))}
    lock = threading.Lock()
    INTERVIEWS_USED[name] = 0

    @tool
    def list_documents() -> str:
        """List this company's documents (annual letter, earnings calls, news) with their dates."""
        lines = []
        for doc_name, path in documents.items():
            text = path.read_text(encoding="utf-8").splitlines()
            dated = next((line for line in text if line.startswith("Date:")), "")
            lines.append(f"{doc_name}: {text[0].lstrip('# ')} ({dated or 'undated'})")
        return "\n".join(lines)

    @tool
    def read_document(name: str) -> str:
        """Return the full text of one document named by list_documents, e.g. 'annual_letter_2025.md'."""
        path = documents.get(name.strip())
        if path is None:
            return f"No document named {name!r}. Available: {', '.join(documents)}"
        return path.read_text(encoding="utf-8")

    @tool
    def sql_query(query: str) -> str:
        """Run one read-only SQLite query on this company's database and return at most 50 rows.

        Amounts are in USD thousands. Dates are 'YYYY-MM-DD', months 'YYYY-MM', quarters like '2025Q3'.
        Tables:
          company(name, industry)
          industry_medians(industry, revenue_growth_2y_pct, new_product_share_pct, new_product_sales_per_rd, operating_margin_pct)
          orders(order_id, order_date, product, customer, amount, note)
          products(product, launch_date)
          monthly_costs(month, cost_of_sales, selling_admin, research_development)
          quarterly_cash(quarter, operating_cash_flow, capex, cash_end)
          capex_plan(year, planned_capex)
          returns(month, units_shipped, units_returned)
          insider_trades(trade_date, person, title, side, shares, price)
        Use SUM, GROUP BY and WHERE to compute totals rather than reading raw rows.
        """
        try:
            con = sqlite3.connect(db_uri, uri=True)
            try:
                cursor = con.execute(query)
                columns = [c[0] for c in cursor.description or []]
                rows = cursor.fetchmany(MAX_SQL_ROWS + 1)
            finally:
                con.close()
        except sqlite3.Error as exc:
            return f"SQL error: {exc}"
        if not columns:
            return "Query returned no columns."
        fmt = lambda v: f"{v:.2f}" if isinstance(v, float) else str(v)  # noqa: E731
        out = [" | ".join(columns)] + [" | ".join(fmt(v) for v in row) for row in rows[:MAX_SQL_ROWS]]
        if len(rows) > MAX_SQL_ROWS:
            out.append(f"... more rows not shown (limit {MAX_SQL_ROWS}); aggregate or add LIMIT/OFFSET.")
        if not rows:
            out.append("(no rows)")
        return "\n".join(out)

    @tool
    def interview(role: str, topic: str) -> str:
        """Interview one person about the company (at most 10 interviews per company).

        role: customer, current_employee, former_employee, competitor or supplier.
        topic: sales_team, working_conditions, top_management, decision_making or competitive_edge.
        People who would not know about a topic say so; the README lists who knows what.
        """
        role, topic = role.strip().lower(), topic.strip().lower()
        if role not in answers:
            return f"Unknown role {role!r}. Roles: {', '.join(answers)}. (Not counted.)"
        if topic not in answers[role]:
            return f"Unknown topic {topic!r}. Topics: {', '.join(answers[role])}. (Not counted.)"
        with lock:
            if INTERVIEWS_USED[name] >= INTERVIEW_LIMIT:
                return f"Interview limit reached: {INTERVIEW_LIMIT} interviews per company."
            INTERVIEWS_USED[name] += 1
        return answers[role][topic]

    return [list_documents, read_document, sql_query, interview]


def normalize_verdict(value: Any) -> str | None:
    """Accept PASS/FAIL (any case) or a boolean."""
    if isinstance(value, bool):
        return "PASS" if value else "FAIL"
    text = str(getattr(value, "content", value)).strip().upper()
    return text if text in VERDICTS else None


def check_chosen_points(chosen: Any) -> list[int]:
    """CHOSEN_POINTS must name 3 different points between 1 and 15."""
    try:
        points = sorted({int(p) for p in chosen})
    except (TypeError, ValueError):
        points = []
    if len(points) != 3 or len(chosen) != 3 or not all(p in FISHER_POINTS for p in points):
        raise SystemExit("Set CHOSEN_POINTS at the top of hw3.py to 3 different point numbers "
                         f"between 1 and 15, e.g. [1, 7, 15] (now: {chosen!r}).")
    return points


def normalize_points(points: Any, chosen: list[int]) -> dict[int, str | None]:
    """Map each chosen point number to PASS/FAIL, or None when missing/invalid."""
    points = points if isinstance(points, dict) else {}
    by_number: dict[int, Any] = {}
    for key, value in points.items():
        try:
            by_number[int(key)] = value
        except (TypeError, ValueError):
            continue
    return {p: normalize_verdict(by_number.get(p)) for p in chosen}


def read_ground_truth(folder: Path) -> dict[str, dict[str, Any]]:
    """Read the expected 15 point verdicts per company."""
    path = folder / "ground_truth.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def point_errors(points: dict[int, str | None], expected: dict[str, Any] | None) -> tuple[int, str]:
    """Count correct point verdicts and describe the wrong ones."""
    if expected is None:
        return 0, ""
    wrong = []
    for p in points:
        truth = expected["points"][str(p)]
        if points[p] != truth:
            wrong.append(f"{p}: expected {truth}, predicted {points[p] or 'missing'}")
    if all(v is None for v in points.values()):
        return 0, "all point verdicts missing"
    return len(points) - len(wrong), "; ".join(wrong)


def balanced_point_accuracy(
    analyses: dict[str, Any], truth: dict[str, dict[str, Any]], names: list[str], chosen: list[int]
) -> float:
    """Mean of the accuracy on true-PASS points and on true-FAIL points.

    Most verdicts are PASS, so answering PASS everywhere gets a high plain
    accuracy but only 50% balanced accuracy. Grading uses the balanced number.
    """
    hits = {"PASS": [0, 0], "FAIL": [0, 0]}
    for name in names:
        if name not in truth:
            continue
        entry = analyses.get(name)
        points = normalize_points(entry.get("points") if isinstance(entry, dict) else None, chosen)
        for p in chosen:
            expected = truth[name]["points"][str(p)]
            hits[expected][0] += points[p] == expected
            hits[expected][1] += 1
    rates = [h / n for h, n in hits.values() if n]
    return sum(rates) / len(rates) if rates else 0.0


def write_results(
    names: list[str], analyses: dict[str, Any], truth: dict[str, dict[str, Any]], chosen: list[int]
) -> tuple[Path, int]:
    """Write results.csv and return the number of correct point verdicts."""
    output = Path("results.csv")
    correct_points = 0
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["company", "points", "points_correct", "point_errors", "interviews_used", "summary"])
        for name in names:
            entry = analyses.get(name)
            entry = entry if isinstance(entry, dict) else {}
            points = normalize_points(entry.get("points"), chosen)
            expected = truth.get(name)
            n_points, errors = point_errors(points, expected)
            correct_points += n_points
            writer.writerow([
                name,
                " ".join(f"{p}:{points[p] or 'missing'}" for p in chosen),
                f"{n_points}/{len(chosen)}" if expected else "",
                errors,
                INTERVIEWS_USED.get(name, 0),
                str(entry.get("summary", "")).strip(),
            ])
    return output, correct_points


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run FTEC5660 HW3 on company data folders")
    parser.add_argument(
        "--data-folder",
        required=True,
        type=Path,
        help="folder containing one sub-folder per company",
    )
    return parser.parse_args()


async def run(folder: Path) -> int:
    folders = company_folders(folder)
    if not folders:
        raise SystemExit(f"no company folders found in {folder}")

    chosen = check_chosen_points(CHOSEN_POINTS)
    load_env_file()
    companies = {path.name: make_tools(path) for path in folders}
    agent = build_agent()
    analyses = await analyze_companies(agent, companies)
    if not isinstance(analyses, dict):
        raise TypeError("analyze_companies() must return a dictionary")

    truth = read_ground_truth(folder)
    output, correct_points = write_results(list(companies), analyses, truth, chosen)
    summary = f" Your points: {', '.join(map(str, chosen))}."
    if truth:
        summary += (
            f" Point accuracy: {correct_points}/{len(companies) * len(chosen)}"
            f" (balanced: {balanced_point_accuracy(analyses, truth, list(companies), chosen):.1%})."
        )
    print(f"Processed {len(companies)} compan{'y' if len(companies) == 1 else 'ies'}. Wrote {output}.{summary}")
    return 0


def main() -> int:
    args = parse_args()
    if not args.data_folder.is_dir():
        raise SystemExit(f"not a folder: {args.data_folder}")
    return asyncio.run(run(args.data_folder))


if __name__ == "__main__":
    raise SystemExit(main())

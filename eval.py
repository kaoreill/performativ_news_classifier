"""Evaluation suite: real article URLs covering the taxonomy and each failure mode.

Every URL here was verified to behave as recorded when the set was assembled.
Live URLs rot, and publishers change their bot policies, so a case that starts
failing on retrieval is a fact about the web rather than a regression in the
classifier — the report prints the retrieval path (direct vs reader) for each
case so the two can be told apart.

Run with:  python eval.py
"""

import asyncio
from collections import Counter

from dotenv import load_dotenv

from app.classifier import classify_article
from app.fetcher import Article, fetch_and_extract

load_dotenv()


EVAL_CASES = [
    # ---- Relevant + negative: a competitor gains ground ----------------------
    #
    # These were originally labelled GOOD_NEWS on the reasoning that wealth-tech
    # activity is good for wealth tech. Under an explicit business-impact rubric
    # the classifier reads them as a strengthened rival, and that is the better
    # reading: category validation is diffuse, a better-funded competitor is
    # concrete. The expectations were corrected to match the argument, not the
    # other way round -- see README, "Encoding business impact".
    {
        "name": "WealthAi launches adviser platform",
        "url": "https://fintech.global/2026/09/03/wealthai-launches-ai-platform-for-independent-advisers/",
        "expected": "BAD_NEWS",
        "category": "Competitor product launch",
    },
    {
        # The least direct of the three: advisor-transition tooling is arguably
        # adjacent rather than competing. It is also the least stable case in
        # this group, which is the honest outcome for a borderline vendor.
        "name": "Dispatch launches advisor transitions software",
        "url": "https://fintech.global/2026/05/29/dispatch-launches-advisor-transitions-software-for-wealth-firms/",
        "expected": "BAD_NEWS",
        "category": "Competitor product launch (borderline)",
    },
    {
        "name": "Wealth.com raises $65M Series B",
        "url": "https://www.wealth.com/resources/press/wealth-com-raises-65-million-series-b-to-power-ai-future-of-wealth-management/",
        "expected": "BAD_NEWS",
        "category": "Competitor funding round",
    },
    # ---- Relevant + positive: regulation drives demand -----------------------
    #
    # Also originally mislabelled, in the opposite direction. Regulatory burden
    # on wealth managers increases demand for exactly the compliance and
    # reporting tooling Performativ sells. The project brief warns against the
    # assumption these labels encoded: "An article about new financial
    # regulation is not automatically bad."
    {
        "name": "Hidden costs of regulatory compliance",
        "url": "https://www.fefundinfo.com/insights/the-hidden-costs-of-regulatory-compliance-what-every-asset-manager-should-know",
        "expected": "GOOD_NEWS",
        "category": "Regulatory demand driver",
    },
    {
        "name": "Asset managers face tighter SEC regulation",
        "url": "https://rsmus.com/insights/industries/asset-management/asset-managers-face-tighter-sec-regulation.html",
        "expected": "GOOD_NEWS",
        "category": "Regulatory demand driver",
    },
    {
        "name": "Rising cost of compliance for banks",
        "url": "https://www.ncontracts.com/nsight-blog/cost-of-compliance-and-how-the-best-banks-respond",
        "expected": "GOOD_NEWS",
        "category": "Regulatory demand driver",
    },
    {
        # A compliance vendor's own blog, framing compliance spend as an
        # optimisable cost. Retained because its framing is promotional while
        # its underlying development is the same as the three above: the rubric
        # should reach the same label from either presentation.
        "name": "Bank compliance spending trends (promotional framing)",
        "url": "https://www.fourthline.com/blog/how-much-do-banks-spend-on-compliance",
        "expected": "GOOD_NEWS",
        "category": "Regulatory demand driver (promotional framing)",
    },
    # ---- Unrelated -----------------------------------------------------------
    {
        "name": "Sports coverage",
        "url": "https://www.bbc.com/sport",
        "expected": "UNRELATED",
        "category": "Sport",
    },
    {
        "name": "General consumer AI coverage",
        "url": "https://techcrunch.com/category/artificial-intelligence/",
        "expected": "UNRELATED",
        "category": "Consumer AI (relevance trap)",
    },
    {
        "name": "General business/macro coverage",
        "url": "https://apnews.com/hub/business",
        "expected": "UNRELATED",
        "category": "Macro news (relevance trap)",
    },
    {
        "name": "Encyclopedia entry, not a news event",
        "url": "https://en.wikipedia.org/wiki/Wealth_management",
        "expected": "UNRELATED",
        "category": "On-topic subject, no news content",
    },
    {
        # A section front, not an article. The service does not attempt to tell
        # index pages from articles (see README: no reliable signal was found),
        # so it processes them when meaningful text is available. Retrieval here
        # goes through the reader and yields mostly navigation.
        "name": "Section front, processed as available text",
        "url": "https://www.reuters.com/technology/",
        "expected": "UNRELATED",
        "category": "Index page, not an article",
    },
    # ---- Failure modes -------------------------------------------------------
    #
    # Named for the property each demonstrates rather than for a publisher.
    # An earlier case asserted that Reuters "hard-blocks automated clients" and
    # expected `http_error`; with an authenticated reader it now retrieves, and
    # Investopedia — the other supposed hard-block — now serves the direct fetch
    # too. Anti-bot posture is the publisher's to change at any time, so no case
    # asserts it. What is asserted below is our own behaviour.
    {
        "name": "Non-HTML resource is rejected (PDF, via response headers)",
        "url": "https://www.occ.gov/publications-and-resources/publications/comptrollers-handbook/files/asset-management/pub-ch-asset-management.pdf",
        "expected": "unsupported_content_type",
        "category": "Failure mode",
    },
    {
        "name": "Machine data is rejected, not classified as an article",
        "url": "https://api.github.com/repos/python/cpython",
        "expected": "unsupported_content_type",
        "category": "Failure mode",
    },
    {
        "name": "Malformed URL is rejected before any fetch",
        "url": "not-a-url",
        "expected": "invalid_url",
        "category": "Failure mode",
    },
    {
        "name": "SSRF target is blocked (cloud metadata endpoint)",
        "url": "http://169.254.169.254/latest/meta-data/",
        "expected": "blocked_url",
        "category": "Failure mode",
    },
    {
        "name": "Unresolvable host fails as a transport error",
        "url": "https://this-domain-definitely-does-not-exist-xyz123.com/article",
        "expected": "fetch_failed",
        "category": "Failure mode",
    },
]


def safe(text: str) -> str:
    """Article text carries typographic characters the Windows console cannot encode."""
    return str(text).encode("ascii", "replace").decode("ascii")


async def run_case(case: dict) -> dict:
    """Run one case, returning the observed outcome and how it was retrieved."""
    article = await fetch_and_extract(case["url"])

    if not isinstance(article, Article):
        return {"result": article.error, "detail": article.detail, "source": "-"}

    classification = await classify_article(article.title, article.text)
    if "error" in classification:
        return {
            "result": classification["error"],
            "detail": classification.get("detail", ""),
            "source": article.source,
        }

    return {
        "result": classification["label"],
        "detail": classification["reasoning"],
        "source": article.source,
    }


async def run_eval() -> None:
    print("Performativ News Classifier - Evaluation Suite")
    print("=" * 78)

    outcomes = Counter()
    rows = []

    for i, case in enumerate(EVAL_CASES, 1):
        try:
            observed = await run_case(case)
        except Exception as e:  # a crash is itself an eval result worth showing
            observed = {"result": f"CRASH: {type(e).__name__}", "detail": str(e)[:120], "source": "-"}

        status = "PASS" if observed["result"] == case["expected"] else "DIFF"
        outcomes[status] += 1
        rows.append((status, case, observed))

        print(f"\n{i:>2}. {case['name']}  [{case['category']}]")
        print(f"    {case['url'][:70]}")
        print(f"    expected={case['expected']:<26} got={observed['result']:<26} [{status}]")
        print(f"    retrieved via: {observed['source']}")
        if observed["detail"]:
            print(f"    {safe(observed['detail'])[:150]}")

    total = outcomes["PASS"] + outcomes["DIFF"]
    print("\n" + "=" * 78)
    print(f"Matched expectation: {outcomes['PASS']}/{total}"
          f" ({100 * outcomes['PASS'] / total:.0f}%)" if total else "no cases run")

    if outcomes["DIFF"]:
        print("\nDivergences:")
        for status, case, observed in rows:
            if status == "DIFF":
                print(f"  - {case['name']}: expected {case['expected']}, got {observed['result']}")


if __name__ == "__main__":
    asyncio.run(run_eval())

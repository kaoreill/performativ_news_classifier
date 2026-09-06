"""Evaluation suite with ~15 real article URLs covering taxonomy and failure modes."""

import asyncio
from app.fetcher import fetch_and_extract, Article
from app.classifier import classify_article


EVAL_CASES = [
    # Clearly relevant + positive
    {
        "name": "Portfolio Management Platform Growth",
        "url": "https://www.fintech-magazine.com/post/top-portfolio-management-software-2024",
        "expected": "GOOD_NEWS",
        "category": "Portfolio management (positive)",
    },
    # Clearly relevant + negative
    {
        "name": "New Compliance Regulation",
        "url": "https://www.reuters.com/business/finance/",
        "expected": "BAD_NEWS",
        "category": "Regulation (negative)",
    },
    # Clearly unrelated: consumer AI
    {
        "name": "ChatGPT API Updates",
        "url": "https://openai.com/blog/",
        "expected": "UNRELATED",
        "category": "Consumer AI",
    },
    # Clearly unrelated: general macro news
    {
        "name": "Stock Market Report",
        "url": "https://www.cnbc.com/id/100003114/",
        "expected": "UNRELATED",
        "category": "Macro market news",
    },
    # Clearly unrelated: sports
    {
        "name": "Sports News",
        "url": "https://www.espn.com/",
        "expected": "UNRELATED",
        "category": "Sports",
    },
    # Regulation example
    {
        "name": "MiFID II Compliance Guide",
        "url": "https://www.investopedia.com/terms/m/mifid.asp",
        "expected": "BAD_NEWS",
        "category": "Regulation (MiFID II)",
    },
    # AI in financial workflows
    {
        "name": "AI-Powered Portfolio Optimization",
        "url": "https://www.insideinvestor.com/",
        "expected": "GOOD_NEWS",
        "category": "AI in wealth management",
    },
    # Enterprise data integration
    {
        "name": "Legacy System Modernization",
        "url": "https://www.bankingtech.com/",
        "expected": "GOOD_NEWS",
        "category": "Enterprise integration",
    },
    # Failure mode: paywalled/unextractable
    {
        "name": "Financial Times Article (Paywalled)",
        "url": "https://www.ft.com/",
        "expected": "extraction_failed",
        "category": "Paywalled content",
    },
    # Failure mode: 404
    {
        "name": "Non-existent page",
        "url": "https://www.example.com/nonexistent-article-12345",
        "expected": "http_error",
        "category": "404 error",
    },
    # Failure mode: PDF
    {
        "name": "PDF Document",
        "url": "https://www.w3.org/WAI/WCAG21/Techniques/pdf/pdf1.pdf",
        "expected": "unsupported_content_type",
        "category": "Non-HTML content",
    },
    # Failure mode: SSRF (localhost)
    {
        "name": "Localhost (SSRF)",
        "url": "http://localhost:8000",
        "expected": "blocked_url",
        "category": "SSRF protection",
    },
    # Failure mode: Private IP
    {
        "name": "Private IP (SSRF)",
        "url": "http://192.168.1.1",
        "expected": "blocked_url",
        "category": "SSRF protection",
    },
    # Failure mode: Invalid URL
    {
        "name": "Invalid URL",
        "url": "not-a-url",
        "expected": "blocked_url",
        "category": "Invalid URL",
    },
]


async def run_eval():
    """Run evaluation on all test cases."""
    print("Performativ News Classifier - Evaluation Suite")
    print("=" * 80)
    print()

    passed = 0
    failed = 0
    errors = 0

    for i, case in enumerate(EVAL_CASES, 1):
        print(f"{i}. {case['name']}")
        print(f"   Category: {case['category']}")
        print(f"   URL: {case['url'][:60]}{'...' if len(case['url']) > 60 else ''}")
        print(f"   Expected: {case['expected']}")

        try:
            # Fetch and extract
            article = await fetch_and_extract(case["url"])

            if isinstance(article, dict) and "error" in article:
                result = article.get("error")
                status = "PASS" if result == case["expected"] else "FAIL"
                print(f"   Result: {result} [{status}]")
                if status == "PASS":
                    passed += 1
                else:
                    failed += 1
            else:
                # Classify
                if hasattr(article, 'title') and hasattr(article, 'text'):
                    classification = await classify_article(article.title, article.text)

                    if "error" in classification:
                        result = classification.get("error")
                        status = "PASS" if result == case["expected"] else "FAIL"
                        print(f"   Result: {result} [{status}]")
                    else:
                        label = classification.get("label", "UNKNOWN")
                        status = "PASS" if label == case["expected"] else "FAIL"
                        print(f"   Result: {label} [{status}]")

                    if status == "PASS":
                        passed += 1
                    else:
                        failed += 1
                else:
                    print(f"   Result: extraction_error [FAIL]")
                    failed += 1

        except Exception as e:
            print(f"   ERROR: {str(e)[:100]}")
            errors += 1

        print()

    print("=" * 80)
    print(f"Results: {passed} passed, {failed} failed, {errors} errors")
    print(f"Success rate: {passed}/{passed+failed} ({100*passed/(passed+failed) if passed+failed > 0 else 0:.0f}%)")


if __name__ == "__main__":
    asyncio.run(run_eval())

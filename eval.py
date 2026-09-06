"""Evaluation suite with ~15 test cases."""
import asyncio
from app.fetcher import fetch_and_extract
from app.classifier import classify_article

CASES = [
    ("Portfolio Mgmt Platform", "https://www.fintech-magazine.com/", "GOOD_NEWS", "Portfolio management"),
    ("Regulation News", "https://www.reuters.com/business/", "BAD_NEWS", "Regulation"),
    ("Consumer AI", "https://openai.com/blog/", "UNRELATED", "Consumer AI"),
    ("Stock Market", "https://www.cnbc.com/", "UNRELATED", "Macro news"),
    ("Sports", "https://www.espn.com/", "UNRELATED", "Sports"),
    ("MiFID II", "https://www.investopedia.com/terms/m/mifid.asp", "BAD_NEWS", "Regulation"),
    ("AI Portfolio", "https://www.insideinvestor.com/", "GOOD_NEWS", "AI in wealth mgmt"),
    ("Legacy Modernization", "https://www.bankingtech.com/", "GOOD_NEWS", "Enterprise integration"),
    ("Paywalled", "https://www.ft.com/", "extraction_failed", "Paywalled"),
    ("404", "https://example.com/nonexistent-12345", "http_error", "404"),
    ("PDF", "https://www.w3.org/WAI/WCAG21/Techniques/pdf/pdf1.pdf", "unsupported_content_type", "PDF"),
    ("Localhost", "http://localhost:8000", "blocked_url", "SSRF"),
    ("Private IP", "http://192.168.1.1", "blocked_url", "SSRF"),
    ("Invalid", "not-a-url", "blocked_url", "Invalid"),
]

async def run_eval():
    print("Evaluation Suite"); print("=" * 60)
    passed = failed = 0
    for name, url, expected, cat in CASES:
        article = await fetch_and_extract(url)
        if isinstance(article, dict) and "error" in article:
            result = article.get("error")
        else:
            if hasattr(article, 'title'):
                clf = await classify_article(article.title, article.text)
                result = clf.get("label") if "error" not in clf else clf.get("error")
            else:
                result = "FAIL"
        status = "PASS" if result == expected else "FAIL"
        if status == "PASS": passed += 1
        else: failed += 1
        print(f"{name:20} {result:20} [{status}]")
    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed ({100*passed/(passed+failed) if passed+failed > 0 else 0:.0f}%)")

if __name__ == "__main__":
    asyncio.run(run_eval())

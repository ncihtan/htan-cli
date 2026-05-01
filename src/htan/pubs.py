"""Search HTAN publications on PubMed and PubMed Central.

Uses NCBI E-utilities REST API — no external dependencies required (stdlib only).

Usage as library::

    from htan.pubs import search, fetch, fulltext
    articles = search(keyword="spatial transcriptomics", max_results=10)
    details = fetch("12345678")

Usage as CLI::

    htan pubs search --keyword "spatial transcriptomics"
    htan pubs fetch 12345678
    htan pubs fulltext "tumor microenvironment"
"""

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import click


EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
TOOL_NAME = "htan_skill"
TOOL_EMAIL = "htan-skill@example.com"
REQUEST_DELAY = 0.34  # seconds between requests (3 req/sec limit without API key)
DEFAULT_TIMEOUT = 60

# HTAN Phase 1 grant numbers (CA233xxx series)
PHASE1_GRANTS = [
    "CA233195", "CA233238", "CA233243", "CA233254", "CA233262",
    "CA233280", "CA233284", "CA233285", "CA233291", "CA233303", "CA233311",
]

# HTAN Phase 2 grant numbers (CA294xxx series)
PHASE2_GRANTS = [
    "CA294459", "CA294507", "CA294514", "CA294518", "CA294527",
    "CA294532", "CA294536", "CA294548", "CA294551", "CA294552",
]

# DCC contract
DCC_GRANTS = ["HHSN261201500003I"]

ALL_GRANTS = PHASE1_GRANTS + PHASE2_GRANTS + DCC_GRANTS

# HTAN-affiliated last authors (PIs)
HTAN_AUTHORS = [
    "Achilefu S", "Ashenberg O", "Aster J", "Cerami E", "Coffey RJ",
    "Curtis C", "Demir E", "Ding L", "Dubinett S", "Esplin ED",
    "Fields R", "Ford JM", "Ghosh S", "Gillanders W", "Goecks J",
    "Gray JW", "Greenleaf W", "Guinney J", "Hanlon SE", "Hughes SK",
    "Hunger SE", "Hupalowska A", "Hwang ES", "Iacobuzio-Donahue CA",
    "Jane-Valbuena J", "Johnson BE", "Lau KS", "Lively T", "Maley C",
    "Mazzilli SA", "Mills GB", "Nawy T", "Oberdoerffer P", "Pe'er D",
    "Regev A", "Rood JE", "Rozenblatt-Rosen O", "Santagata S",
    "Schapiro D", "Shalek AK", "Shrubsole MJ", "Snyder MP",
    "Sorger PK", "Spira AE", "Srivastava S", "Suva M", "Tan K",
    "Thomas GV", "West RB", "Williams EH", "Wold B", "Bastian B",
    "Dos Santos DC", "Fertig E", "Chen F", "Shain AH", "Ghobrial I",
    "Yeh I", "Amatruda J", "Spraggins J", "Brody J", "Wood L",
    "Wang L", "Cai L", "Shrubsole M", "Thomson M", "Birrer M",
    "Xu M", "Li M", "Mansfield P", "Everson R", "Fan R",
    "Sears R", "Pachynski R", "Fields R", "Mok S",
    "Ferri-Borgogno S", "Asgharzadeh S", "Halene S", "Hwang TH", "Ma Z",
]


def build_grant_query():
    """Build PubMed grant number query string."""
    return " OR ".join(f"{g}[gr]" for g in ALL_GRANTS)


def build_author_query(author=None):
    """Build PubMed author query. If author is given, filter to that author only."""
    if author:
        return f"{author}[LASTAU]"
    return " OR ".join(f"{a}[LASTAU]" for a in HTAN_AUTHORS)


def build_search_query(keyword=None, author=None, year=None):
    """Build a combined PubMed search query for HTAN publications."""
    grant_query = build_grant_query()
    author_query = build_author_query(author)
    query = f"({grant_query}) AND ({author_query})"
    if keyword:
        query = f"({query}) AND ({keyword})"
    if year:
        query = f"({query}) AND ({year}[pdat])"
    return query


def eutils_request(endpoint, params, timeout=DEFAULT_TIMEOUT):
    """Make a rate-limited request to NCBI E-utilities."""
    params["tool"] = TOOL_NAME
    params["email"] = TOOL_EMAIL
    url = f"{EUTILS_BASE}/{endpoint}?{urllib.parse.urlencode(params)}"
    time.sleep(REQUEST_DELAY)
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        print(f"Error: HTTP {e.code} from E-utilities: {e.reason}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Error: Could not connect to E-utilities: {e.reason}", file=sys.stderr)
        sys.exit(1)
    except TimeoutError:
        print(f"Error: PubMed request timed out after {timeout}s.", file=sys.stderr)
        sys.exit(1)


def _parse_article_xml(article_elem):
    """Parse a PubmedArticle XML element into a dict."""
    try:
        medline = article_elem.find("MedlineCitation")
        if medline is None:
            return None

        pmid_elem = medline.find("PMID")
        pmid = pmid_elem.text if pmid_elem is not None else ""

        article = medline.find("Article")
        if article is None:
            return None

        title_elem = article.find("ArticleTitle")
        title = "".join(title_elem.itertext()) if title_elem is not None else ""

        authors = []
        author_list = article.find("AuthorList")
        if author_list is not None:
            for author_elem in author_list.findall("Author"):
                last = author_elem.find("LastName")
                initials = author_elem.find("Initials")
                if last is not None:
                    name = last.text
                    if initials is not None:
                        name += f" {initials.text}"
                    authors.append(name)

        journal_elem = article.find("Journal/Title")
        journal = journal_elem.text if journal_elem is not None else ""

        year = ""
        pub_date = article.find("Journal/JournalIssue/PubDate")
        if pub_date is not None:
            year_elem = pub_date.find("Year")
            if year_elem is not None:
                year = year_elem.text
            else:
                medline_date = pub_date.find("MedlineDate")
                if medline_date is not None and medline_date.text:
                    year = medline_date.text[:4]

        abstract_parts = []
        abstract_elem = article.find("Abstract")
        if abstract_elem is not None:
            for abs_text in abstract_elem.findall("AbstractText"):
                label = abs_text.get("Label", "")
                text = "".join(abs_text.itertext()) or ""
                if label:
                    abstract_parts.append(f"{label}: {text}")
                else:
                    abstract_parts.append(text)
        abstract = "\n".join(abstract_parts)

        doi = ""
        article_id_list = article_elem.find("PubmedData/ArticleIdList")
        if article_id_list is not None:
            for aid in article_id_list.findall("ArticleId"):
                if aid.get("IdType") == "doi":
                    doi = aid.text or ""
                    break

        return {
            "pmid": pmid, "title": title, "authors": authors,
            "journal": journal, "year": year, "doi": doi, "abstract": abstract,
        }
    except Exception as e:
        print(f"Warning: Failed to parse article: {e}", file=sys.stderr)
        return None


# --- Public API ---

def search(keyword=None, author=None, year=None, max_results=100, timeout=DEFAULT_TIMEOUT):
    """Search HTAN publications on PubMed.

    Args:
        keyword: Optional keyword filter.
        author: Optional last author filter (e.g., "Sorger PK").
        year: Optional publication year filter.
        max_results: Maximum number of results (default 100).
        timeout: HTTP timeout in seconds.

    Returns:
        List of article dicts with keys: pmid, title, authors, journal, year, doi, abstract.
    """
    query = build_search_query(keyword=keyword, author=author, year=year)
    params = {
        "db": "pubmed", "term": query, "retmax": str(max_results),
        "retmode": "json", "sort": "pub_date",
    }
    raw = eutils_request("esearch.fcgi", params, timeout=timeout)
    data = json.loads(raw)
    result = data.get("esearchresult", {})
    count = int(result.get("count", 0))
    pmids = result.get("idlist", [])
    print(f"Found {count} results, returning {len(pmids)}", file=sys.stderr)

    if not pmids:
        return []

    return fetch(pmids, timeout=timeout)


def fetch(pmids, timeout=DEFAULT_TIMEOUT):
    """Fetch article details for a list of PMIDs.

    Args:
        pmids: Single PMID string or list of PMIDs.
        timeout: HTTP timeout in seconds.

    Returns:
        List of article dicts.
    """
    if isinstance(pmids, str):
        pmids = [pmids]
    if not pmids:
        return []

    articles = []
    batch_size = 200
    for i in range(0, len(pmids), batch_size):
        batch = pmids[i : i + batch_size]
        params = {"db": "pubmed", "id": ",".join(batch), "rettype": "xml", "retmode": "xml"}
        raw = eutils_request("efetch.fcgi", params, timeout=timeout)
        root = ET.fromstring(raw)
        for article_elem in root.findall(".//PubmedArticle"):
            article = _parse_article_xml(article_elem)
            if article:
                articles.append(article)

    return articles


def fulltext(query, max_results=50, timeout=DEFAULT_TIMEOUT):
    """Search HTAN articles in PubMed Central (full-text).

    Args:
        query: Full-text search query.
        max_results: Maximum number of results.
        timeout: HTTP timeout in seconds.

    Returns:
        List of article dicts with keys: pmc_id, title, journal, year, doi, pmid, authors.
    """
    grant_query = build_grant_query()
    full_query = f"({grant_query}) AND ({query})"

    params = {
        "db": "pmc", "term": full_query, "retmax": str(max_results),
        "retmode": "json", "sort": "pub_date",
    }
    raw = eutils_request("esearch.fcgi", params, timeout=timeout)
    data = json.loads(raw)
    result = data.get("esearchresult", {})
    count = int(result.get("count", 0))
    pmc_ids = result.get("idlist", [])
    print(f"Found {count} PMC results, returning {len(pmc_ids)}", file=sys.stderr)

    if not pmc_ids:
        return []

    params = {"db": "pmc", "id": ",".join(pmc_ids), "retmode": "json"}
    raw = eutils_request("esummary.fcgi", params, timeout=timeout)
    data = json.loads(raw)
    summaries = data.get("result", {})

    articles = []
    for pmc_id in pmc_ids:
        info = summaries.get(pmc_id, {})
        if not isinstance(info, dict):
            continue
        articles.append({
            "pmc_id": f"PMC{pmc_id}",
            "title": info.get("title", ""),
            "journal": info.get("fulljournalname", info.get("source", "")),
            "year": info.get("pubdate", "")[:4],
            "doi": "",
            "pmid": info.get("articleids", [{}])[0].get("value", "") if info.get("articleids") else "",
            "authors": [a.get("name", "") for a in info.get("authors", [])],
        })

    return articles


# --- Formatting ---

def format_article_text(article):
    """Format a single article for text output."""
    lines = []
    pmid = article.get("pmid", "")
    pmc_id = article.get("pmc_id", "")
    identifier = f"PMID: {pmid}" if pmid else f"PMC: {pmc_id}"
    lines.append(f"{identifier}")
    lines.append(f"  Title: {article.get('title', 'N/A')}")
    authors = article.get("authors", [])
    if authors:
        if len(authors) > 5:
            author_str = ", ".join(authors[:5]) + f", ... (+{len(authors)-5} more)"
        else:
            author_str = ", ".join(authors)
        lines.append(f"  Authors: {author_str}")
    lines.append(f"  Journal: {article.get('journal', 'N/A')} ({article.get('year', 'N/A')})")
    if article.get("doi"):
        lines.append(f"  DOI: https://doi.org/{article['doi']}")
    if article.get("abstract"):
        abstract = article["abstract"]
        if len(abstract) > 300:
            abstract = abstract[:300] + "..."
        lines.append(f"  Abstract: {abstract}")
    return "\n".join(lines)


# --- CLI ---

_PUBS_EPILOG = """\
Examples:

  htan pubs search
  htan pubs search --keyword "spatial transcriptomics"
  htan pubs search --author "Sorger PK"
  htan pubs fetch 12345678
  htan pubs fulltext "tumor microenvironment"
"""


@click.group(name="pubs", epilog=_PUBS_EPILOG)
def pubs():
    """Search HTAN publications on PubMed and PubMed Central."""


def _print_articles(articles, fmt):
    if not articles:
        click.echo("No articles found.", err=True)
        return
    if fmt == "json":
        click.echo(json.dumps(articles, indent=2))
    else:
        for a in articles:
            click.echo(format_article_text(a))
            click.echo()


@pubs.command(name="search")
@click.option("--keyword", "-k", help="Filter by keyword")
@click.option("--author", "-a", help="Filter by last author")
@click.option("--year", "-y", help="Filter by publication year")
@click.option("--max-results", "-n", "max_results", type=int, default=100, show_default=True)
@click.option("--format", "-f", "fmt", type=click.Choice(["text", "json"]), default="text")
@click.option("--timeout", type=int, default=DEFAULT_TIMEOUT, show_default=True)
@click.option("--dry-run", "dry_run", is_flag=True, help="Show query URL without executing")
def search_cmd(keyword, author, year, max_results, fmt, timeout, dry_run):
    """Search HTAN publications on PubMed."""
    if dry_run:
        query = build_search_query(keyword=keyword, author=author, year=year)
        params = {"db": "pubmed", "term": query, "retmax": str(max_results),
                  "retmode": "json", "sort": "pub_date", "tool": TOOL_NAME, "email": TOOL_EMAIL}
        url = f"{EUTILS_BASE}/esearch.fcgi?{urllib.parse.urlencode(params)}"
        click.echo("Dry run — would request:", err=True)
        click.echo(f"  URL: {url}", err=True)
        return
    articles = search(keyword=keyword, author=author, year=year,
                      max_results=max_results, timeout=timeout)
    _print_articles(articles, fmt)


@pubs.command(name="fetch")
@click.argument("pmids", nargs=-1, required=True)
@click.option("--format", "-f", "fmt", type=click.Choice(["text", "json"]), default="text")
@click.option("--timeout", type=int, default=DEFAULT_TIMEOUT, show_default=True)
@click.option("--dry-run", "dry_run", is_flag=True)
def fetch_cmd(pmids, fmt, timeout, dry_run):
    """Fetch details for specific PMIDs."""
    if dry_run:
        click.echo(f"Dry run — would fetch PMIDs: {', '.join(pmids)}", err=True)
        return
    articles = fetch(list(pmids), timeout=timeout)
    _print_articles(articles, fmt)


@pubs.command(name="fulltext")
@click.argument("query")
@click.option("--max-results", "-n", "max_results", type=int, default=50, show_default=True)
@click.option("--format", "-f", "fmt", type=click.Choice(["text", "json"]), default="text")
@click.option("--timeout", type=int, default=DEFAULT_TIMEOUT, show_default=True)
@click.option("--dry-run", "dry_run", is_flag=True)
def fulltext_cmd(query, max_results, fmt, timeout, dry_run):
    """Search HTAN articles in PubMed Central."""
    if dry_run:
        grant_query = build_grant_query()
        full_query = f"({grant_query}) AND ({query})"
        params = {"db": "pmc", "term": full_query, "retmax": str(max_results),
                  "retmode": "json", "sort": "pub_date", "tool": TOOL_NAME, "email": TOOL_EMAIL}
        url = f"{EUTILS_BASE}/esearch.fcgi?{urllib.parse.urlencode(params)}"
        click.echo("Dry run — would request:", err=True)
        click.echo(f"  URL: {url}", err=True)
        return
    articles = fulltext(query, max_results=max_results, timeout=timeout)
    if not articles:
        click.echo("No PMC articles found.", err=True)
        return
    if fmt == "json":
        click.echo(json.dumps(articles, indent=2))
    else:
        for a in articles:
            click.echo(format_article_text(a))
            click.echo()


def cli_main(argv=None):
    """Backward-compatible entry point — invokes the Click :data:`pubs` group."""
    try:
        return pubs.main(args=argv, prog_name="htan pubs", standalone_mode=False)
    except click.exceptions.Exit as e:
        sys.exit(e.exit_code)
    except click.exceptions.ClickException as e:
        e.show()
        sys.exit(e.exit_code)

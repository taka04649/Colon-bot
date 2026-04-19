"""PubMed E-utilities wrapper.

Provides PubMed search and article fetch with consistent error handling
and rate limiting. Used by multiple bots.
"""

from __future__ import annotations

import os
import time
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from xml.etree import ElementTree as ET

import requests

logger = logging.getLogger(__name__)

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

# 高インパクト誌の優先ソート用
HIGH_IMPACT_JOURNALS = [
    "lancet", "nejm", "n engl j med", "new england", "nature", "cell", "science",
    "jama", "bmj",
    "gastroenterology", "gut", "clin gastroenterol hepatol", "am j gastroenterol",
    "lancet gastroenterol hepatol", "nat rev gastroenterol hepatol",
    "gastrointest endosc", "endoscopy",
    "j crohns colitis", "inflamm bowel dis", "aliment pharmacol ther",
    "j clin oncol", "ann oncol", "lancet oncol", "jama oncol",
    "ann surg", "dis colon rectum", "colorectal dis",
]


@dataclass
class Paper:
    """PubMed論文のメタデータ"""
    pmid: str
    title: str
    abstract: str
    journal: str
    pub_date: str
    doi: Optional[str] = None
    authors: Optional[list[str]] = None
    pub_types: Optional[list[str]] = None

    def __post_init__(self):
        if self.authors is None:
            self.authors = []
        if self.pub_types is None:
            self.pub_types = []

    @property
    def url(self) -> str:
        return f"https://pubmed.ncbi.nlm.nih.gov/{self.pmid}/"

    @property
    def is_high_impact(self) -> bool:
        j = self.journal.lower()
        return any(h in j for h in HIGH_IMPACT_JOURNALS)

    @property
    def first_author_str(self) -> str:
        if not self.authors:
            return ""
        s = self.authors[0]
        if len(self.authors) > 1:
            s += " et al."
        return s


def _params(**kwargs) -> dict:
    """E-utilities共通パラメータ"""
    params = {
        "tool": "medical-bots",
        "email": os.environ.get("PUBMED_EMAIL", "example@example.com"),
        **kwargs,
    }
    api_key = os.environ.get("PUBMED_API_KEY")
    if api_key:
        params["api_key"] = api_key
    return params


def search(
    query: str,
    mindate: Optional[datetime] = None,
    maxdate: Optional[datetime] = None,
    retmax: int = 100,
    sort: str = "pub_date",
) -> list[str]:
    """PubMed検索、PMIDリストを返す

    Args:
        query: PubMed検索クエリ(未加工のもの)
        mindate, maxdate: 期間指定。Noneなら期間無制限
        retmax: 最大取得件数
        sort: "pub_date" or "relevance"
    """
    params = _params(
        db="pubmed",
        term=query,
        retmax=retmax,
        retmode="json",
        sort=sort,
    )
    if mindate and maxdate:
        params["mindate"] = mindate.strftime("%Y/%m/%d")
        params["maxdate"] = maxdate.strftime("%Y/%m/%d")
        params["datetype"] = "pdat"

    try:
        r = requests.get(f"{EUTILS_BASE}/esearch.fcgi", params=params, timeout=30)
        r.raise_for_status()
        pmids = r.json().get("esearchresult", {}).get("idlist", [])
        logger.info(f"PubMed search returned {len(pmids)} PMIDs")
        return pmids
    except Exception as e:
        logger.warning(f"PubMed esearch failed: {e}")
        return []


def fetch(pmids: list[str]) -> list[Paper]:
    """PMIDリストから論文詳細を取得"""
    if not pmids:
        return []

    papers: list[Paper] = []
    batch_size = 100
    for i in range(0, len(pmids), batch_size):
        batch = pmids[i:i + batch_size]
        params = _params(db="pubmed", id=",".join(batch), retmode="xml")
        try:
            r = requests.post(f"{EUTILS_BASE}/efetch.fcgi", data=params, timeout=60)
            r.raise_for_status()
        except Exception as e:
            logger.warning(f"PubMed efetch failed for batch {i}: {e}")
            continue

        try:
            root = ET.fromstring(r.content)
        except ET.ParseError as e:
            logger.warning(f"XML parse error: {e}")
            continue

        for article in root.findall(".//PubmedArticle"):
            paper = _parse_article(article)
            if paper:
                papers.append(paper)

        # NCBI rate limit courtesy (3 req/s without API key, 10 req/s with)
        time.sleep(0.4 if not os.environ.get("PUBMED_API_KEY") else 0.15)

    logger.info(f"Fetched {len(papers)} papers")
    return papers


def search_and_fetch(
    query: str,
    mindate: Optional[datetime] = None,
    maxdate: Optional[datetime] = None,
    retmax: int = 100,
    sort: str = "pub_date",
    require_abstract: bool = True,
) -> list[Paper]:
    """検索+取得の統合ヘルパー"""
    pmids = search(query, mindate, maxdate, retmax, sort)
    papers = fetch(pmids)
    if require_abstract:
        papers = [p for p in papers if p.abstract.strip()]
    return papers


def recent_days(query: str, days: int, retmax: int = 100, **kwargs) -> list[Paper]:
    """直近N日の論文を取得する便利関数"""
    date_to = datetime.now(timezone.utc)
    date_from = date_to - timedelta(days=days)
    return search_and_fetch(
        query=query,
        mindate=date_from,
        maxdate=date_to,
        retmax=retmax,
        **kwargs,
    )


def _parse_article(article: ET.Element) -> Optional[Paper]:
    """XML要素から Paper データクラスを構築"""
    try:
        pmid = article.findtext(".//PMID") or ""
        title_elem = article.find(".//ArticleTitle")
        title = "".join(title_elem.itertext()).strip() if title_elem is not None else ""

        # Abstract(複数セクション対応)
        abstract_parts = []
        for abs_elem in article.findall(".//Abstract/AbstractText"):
            label = abs_elem.get("Label", "")
            text = "".join(abs_elem.itertext()).strip()
            if label:
                abstract_parts.append(f"{label}: {text}")
            else:
                abstract_parts.append(text)
        abstract = "\n".join(abstract_parts)

        journal = article.findtext(".//Journal/ISOAbbreviation") \
            or article.findtext(".//Journal/Title") or ""

        # Publication date
        pub_year = article.findtext(".//PubDate/Year") \
            or article.findtext(".//PubDate/MedlineDate", "")[:4] or ""
        pub_month = article.findtext(".//PubDate/Month") or ""
        pub_day = article.findtext(".//PubDate/Day") or ""
        pub_date = "-".join(p for p in [pub_year, pub_month, pub_day] if p)

        # DOI
        doi = None
        for eid in article.findall(".//ArticleId"):
            if eid.get("IdType") == "doi":
                doi = eid.text
                break

        # Authors (先頭3名)
        authors = []
        for author in article.findall(".//Author")[:3]:
            last = author.findtext("LastName") or ""
            init = author.findtext("Initials") or ""
            if last:
                name = f"{last} {init}".strip()
                authors.append(name)

        # Publication types (Review, Randomized Controlled Trial 等)
        pub_types = []
        for pt in article.findall(".//PublicationType"):
            if pt.text:
                pub_types.append(pt.text)

        if not pmid or not title:
            return None

        return Paper(
            pmid=pmid, title=title, abstract=abstract,
            journal=journal, pub_date=pub_date,
            doi=doi, authors=authors, pub_types=pub_types,
        )
    except Exception as e:
        logger.warning(f"Failed to parse article: {e}")
        return None


def sort_by_impact(papers: list[Paper]) -> list[Paper]:
    """高インパクト誌を先頭にソート"""
    return sorted(papers, key=lambda p: (not p.is_high_impact, p.pub_date), reverse=False)

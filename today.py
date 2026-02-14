import datetime
from dateutil import relativedelta
import requests
import os
from lxml import etree
import time
import hashlib

HEADERS = {"authorization": "token " + os.environ["ACCESS_TOKEN"]}
USER_NAME = os.environ["USER_NAME"]

QUERY_COUNT = {
    "user_getter": 0,
    "follower_getter": 0,
    "graph_repos_stars": 0,
    "graph_commits": 0,
    "loc_query": 0,
    "recursive_loc": 0,
}
OWNER_ID = None


def daily_readme(birthday: datetime.datetime) -> str:
    """Return the age in years, months, and days with birthday emoji if applicable."""
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    emoji = " 🎂" if diff.months == 0 and diff.days == 0 else ""
    return (
        f"{diff.years} year{'s' if diff.years != 1 else ''}, "
        f"{diff.months} month{'s' if diff.months != 1 else ''}, "
        f"{diff.days} day{'s' if diff.days != 1 else ''}{emoji}"
    )


def simple_request(func_name: str, query: str, variables: dict) -> requests.Response:
    """
    Send a GraphQL request and return the response.
    Raises a useful exception for HTTP errors, GraphQL errors, or malformed payloads.
    """
    QUERY_COUNT[func_name] += 1
    r = requests.post(
        "https://api.github.com/graphql",
        json={"query": query, "variables": variables},
        headers=HEADERS,
        timeout=30,
    )

    try:
        payload = r.json()
    except Exception:
        raise Exception(f"{func_name} non-json response: {r.status_code} {r.text}")

    if r.status_code != 200:
        raise Exception(f"{func_name} http error: {r.status_code} {payload}")

    # GraphQL can return 200 with errors
    if isinstance(payload, dict) and payload.get("errors"):
        raise Exception(f"{func_name} graphql errors: {payload['errors']}")

    if "data" not in payload:
        raise Exception(f"{func_name} missing data field: {payload}")

    return r


def graph_commits(start_date, end_date) -> int:
    """Fetch total contributions between start_date and end_date."""
    query = """
    query($start_date: DateTime!, $end_date: DateTime!, $login: String!) {
        user(login: $login) {
            contributionsCollection(from: $start_date, to: $end_date) {
                contributionCalendar { totalContributions }
            }
        }
    }"""
    variables = {"start_date": start_date, "end_date": end_date, "login": USER_NAME}
    data = simple_request("graph_commits", query, variables).json().get("data")
    if not data or not data.get("user"):
        return 0
    return int(
        data["user"]["contributionsCollection"]["contributionCalendar"][
            "totalContributions"
        ]
    )


def graph_repos_stars(count_type: str, owner_affiliation: list) -> int:
    """
    Fetch repository count or total stars for the user.
    Correctly paginates through all repositories.
    """
    query = """
    query($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges { node { stargazers { totalCount } } }
                pageInfo { endCursor hasNextPage }
            }
        }
    }"""

    cursor = None
    total_repos = None
    total_stars = 0

    while True:
        variables = {
            "owner_affiliation": owner_affiliation,
            "login": USER_NAME,
            "cursor": cursor,
        }
        data = simple_request("graph_repos_stars", query, variables).json()["data"][
            "user"
        ]["repositories"]

        if total_repos is None:
            total_repos = int(data["totalCount"])

        if count_type == "stars":
            total_stars += sum(
                edge["node"]["stargazers"]["totalCount"] for edge in data["edges"]
            )

        if not data["pageInfo"]["hasNextPage"]:
            break
        cursor = data["pageInfo"]["endCursor"]

    if count_type == "repos":
        return total_repos
    if count_type == "stars":
        return total_stars
    raise ValueError("count_type must be 'repos' or 'stars'")


def recursive_loc(owner, repo_name, cursor=None, additions=0, deletions=0, commits=0):
    """Recursively calculate LOC and commits for a repository."""
    QUERY_COUNT["recursive_loc"] += 1
    query = """
    query($owner: String!, $repo_name: String!, $cursor: String) {
        repository(owner: $owner, name: $repo_name) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            edges {
                                node { additions deletions author { user { id } } }
                            }
                            pageInfo { hasNextPage endCursor }
                        }
                    }
                }
            }
        }
    }"""
    variables = {"owner": owner, "repo_name": repo_name, "cursor": cursor}
    repo = simple_request("recursive_loc", query, variables).json()["data"][
        "repository"
    ]

    if not repo or not repo.get("defaultBranchRef"):
        return additions, deletions, commits

    history = repo["defaultBranchRef"]["target"]["history"]

    for edge in history["edges"]:
        node = edge["node"]
        author = node.get("author")
        user = author.get("user") if author else None
        if user and user.get("id") == OWNER_ID:
            commits += 1
            additions += int(node.get("additions", 0))
            deletions += int(node.get("deletions", 0))

    if history["pageInfo"]["hasNextPage"]:
        return recursive_loc(
            owner,
            repo_name,
            history["pageInfo"]["endCursor"],
            additions,
            deletions,
            commits,
        )

    return additions, deletions, commits


def loc_pipeline():
    """Compute LOC across all repositories using cache and recursive LOC counting."""
    filename = f"cache/{hashlib.sha256(USER_NAME.encode()).hexdigest()}.txt"
    os.makedirs("cache", exist_ok=True)

    edges = []
    query = """
    query($login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor,
              ownerAffiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER]) {
                edges { node { nameWithOwner } }
                pageInfo { endCursor hasNextPage }
            }
        }
    }"""

    cursor = None
    while True:
        response = simple_request(
            "loc_query", query, {"login": USER_NAME, "cursor": cursor}
        ).json()["data"]["user"]["repositories"]
        edges.extend(response["edges"])
        if not response["pageInfo"]["hasNextPage"]:
            break
        cursor = response["pageInfo"]["endCursor"]

    # cache lines correspond to repo order in edges; keep same length
    if os.path.exists(filename):
        with open(filename, "r") as f:
            cache_data = f.readlines()
    else:
        cache_data = []

    # ensure cache_data has right size
    if len(cache_data) < len(edges):
        cache_data.extend(["0 0 0 0\n"] * (len(edges) - len(cache_data)))
    elif len(cache_data) > len(edges):
        cache_data = cache_data[: len(edges)]

    additions_total, deletions_total, commits_total = 0, 0, 0

    for i, repo in enumerate(edges):
        name_with_owner = repo["node"]["nameWithOwner"]
        owner, repo_name = name_with_owner.split("/", 1)

        add, delete, commit = recursive_loc(owner, repo_name)
        additions_total += add
        deletions_total += delete
        commits_total += commit

        cache_data[i] = (
            f"{hashlib.sha256(name_with_owner.encode()).hexdigest()} "
            f"{commit} {add} {delete}\n"
        )

    with open(filename, "w") as f:
        f.writelines(cache_data)

    return (
        additions_total,
        deletions_total,
        additions_total - deletions_total,
        commits_total,
    )


def justify_format(root, element_id, new_text) -> bool:
    """
    Replace text content of an SVG element by id.
    If the element contains tspans, updates the first tspan (common SVG pattern).
    """
    new_text = str(new_text)

    elems = root.xpath(f"//*[@id='{element_id}']")
    if not elems:
        return False

    el = elems[0]
    tspans = el.findall(".//{*}tspan")
    if tspans:
        tspans[0].text = new_text
    else:
        el.text = new_text
    return True


def svg_overwrite(
    filenames,
    age_data,
    commit_data,
    star_data,
    repo_data,
    contrib_data,
    follower_data,
    loc_data,
):
    """
    Update SVG text elements by id, including LOC with added/deleted stats.
    """
    if isinstance(filenames, str):
        filenames = [filenames]

    for filename in filenames:
        if not os.path.exists(filename):
            continue

        parser = etree.XMLParser(remove_blank_text=False)
        tree = etree.parse(filename, parser)
        root = tree.getroot()

        for elem_id, val in zip(
            [
                "age_data",
                "commit_data",
                "star_data",
                "repo_data",
                "contrib_data",
                "follower_data",
            ],
            [age_data, commit_data, star_data, repo_data, contrib_data, follower_data],
        ):
            justify_format(root, elem_id, val)

        # LOC special handling
        if loc_data:
            main_loc, added, deleted = loc_data[2], loc_data[0], loc_data[1]
            justify_format(root, "loc_data", f"{main_loc:,}")
            added_deleted_text = f"(+{added:,} / -{deleted:,})"

            loc_elem = root.xpath("//*[@id='loc_data']")
            if loc_elem:
                parent = loc_elem[0].getparent()
                # If a purple sibling exists, reuse it; else append
                for ts in parent.findall(".//{*}tspan"):
                    style = ts.attrib.get("style", "")
                    if "#bb9af7" in style:
                        ts.text = added_deleted_text
                        break
                else:
                    new_ts = etree.Element("tspan")
                    new_ts.text = added_deleted_text
                    new_ts.attrib["dx"] = "12"
                    new_ts.attrib["style"] = "fill:#bb9af7"
                    parent.append(new_ts)

        temp_filename = filename + ".tmp"
        tree.write(temp_filename, encoding="utf-8", xml_declaration=True)
        os.replace(temp_filename, filename)


def user_getter(username):
    """Fetch GitHub user ID and account creation date."""
    query = "query($login: String!){ user(login: $login) { id createdAt } }"
    data = simple_request("user_getter", query, {"login": username}).json()["data"][
        "user"
    ]
    return data["id"], data["createdAt"]


def follower_getter(username):
    """Fetch follower count."""
    query = "query($login: String!){ user(login: $login) { followers { totalCount } } }"
    return int(
        simple_request("follower_getter", query, {"login": username}).json()["data"][
            "user"
        ]["followers"]["totalCount"]
    )


def perf_counter(func, *args):
    """Measure execution time of a function."""
    start = time.perf_counter()
    result = func(*args)
    return result, time.perf_counter() - start


def is_tuesday():
    """Return True if today is Tuesday."""
    return datetime.datetime.today().weekday() == 1


if __name__ == "__main__":
    try:
        user_id, acc_date = user_getter(USER_NAME)
        OWNER_ID = user_id

        age_data, t_age = perf_counter(daily_readme, datetime.datetime(2006, 12, 12))
        print(f"Age calculation: {t_age:.4f}s")

        stars, t_stars = perf_counter(
            graph_repos_stars, "stars", ["OWNER", "COLLABORATOR", "ORGANIZATION_MEMBER"]
        )
        print(f"Star count: {t_stars:.4f}s")

        repos, t_repos = perf_counter(
            graph_repos_stars, "repos", ["OWNER", "COLLABORATOR", "ORGANIZATION_MEMBER"]
        )
        print(f"Repo count: {t_repos:.4f}s")

        followers, t_followers = perf_counter(follower_getter, USER_NAME)
        print(f"Follower count: {t_followers:.4f}s")

        commits, t_commits = perf_counter(
            graph_commits, acc_date, datetime.datetime.utcnow().isoformat()
        )
        print(f"Commit count: {t_commits:.4f}s")

        loc_data = loc_pipeline() if is_tuesday() else (0, 0, 0, 0)

    except Exception as e:
        print(f"Error encountered: {e}")
        # Keep variables defined for svg_overwrite
        age_data = daily_readme(datetime.datetime(2006, 12, 12))
        stars = repos = followers = commits = 0
        loc_data = (0, 0, 0, 0)

    svg_overwrite(
        ["dark_mode.svg", "light_mode.svg"],
        age_data,
        commits,
        stars,
        repos,
        commits,  # contrib_data (you were using commits here originally)
        followers,
        loc_data,
    )

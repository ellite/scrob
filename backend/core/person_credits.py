"""Merge a person's TMDB credits per title and group them by media kind and credit group."""

from core.credit_order import CREDIT_ORDER, role_group, role_sort_key

_CREW_SCORE_WEIGHT = {"Directing": 1.0, "Writing": 0.9, "Production": 0.7, "Creator": 1.0}


def format_person_credits(credits: dict, poster_url) -> list[dict]:
    """One entry per (kind, tmdb_id), best-scored first, carrying every role in `roles`.

    A movie and a show can share a numeric TMDB id, so the kind is part of the key.
    """
    titles: dict[tuple[str, int], dict] = {}
    for source in ("cast", "crew"):
        for c in credits.get(source, []):
            kind = {"movie": "movie", "tv": "series"}.get(c.get("media_type"))
            tid = c.get("id")
            if not kind or not isinstance(tid, int) or tid <= 0:
                continue
            popularity = c.get("popularity") or 0
            if source == "cast":
                if kind == "series":
                    weight = min(c.get("episode_count") or 0, 20) / 20.0
                else:
                    weight = max(0.05, 1.0 - (c.get("order") or 0) * 0.05)
                role = {"job": "Actor", "department": "Acting", "character": c.get("character") or ""}
            else:
                department = c.get("department") or "Crew"
                weight = _CREW_SCORE_WEIGHT.get(department, 0.5)
                role = {"job": (c.get("job") or department).strip(), "department": department, "character": ""}
            score = popularity * max(weight, 0.05)
            previous = titles.get((kind, tid))
            roles = previous["roles"] if previous else []
            if role not in roles:
                roles.append(role)
            if previous is None or score > previous["_score"]:
                titles[(kind, tid)] = {
                    "tmdb_id": tid,
                    "type": kind,
                    "title": c.get("title") or c.get("name"),
                    "poster_path": poster_url(c.get("poster_path")),
                    "release_date": c.get("release_date") or c.get("first_air_date"),
                    "character": c.get("character") if source == "cast" else role["job"],
                    "popularity": popularity,
                    "adult": c.get("adult", False),
                    "genre_ids": c.get("genre_ids", []),
                    "vote_average": c.get("vote_average") or 0,
                    "_score": score,
                    "roles": roles,
                }
    result = sorted(titles.values(), key=lambda t: (-t["_score"], t["type"], t["tmdb_id"]))
    for item in result:
        del item["_score"]
    return result


def group_person_credits(titles: list[dict]) -> list[dict]:
    """Group merged titles by (kind, credit group): movies before shows, then the order of
    core.credit_order (acting, directing, story, camera, editing, music, the rest, production last).

    A title with roles in several groups appears in each, showing only that group's jobs
    (Actor: the character names), best job first.
    """
    groups: dict[tuple[str, str], list[dict]] = {}
    for title in titles:
        by_group: dict[str, list[dict]] = {}
        for role in sorted(title.get("roles", []), key=role_sort_key):
            by_group.setdefault(role_group(role), []).append(role)
        for group, roles in by_group.items():
            item = {k: v for k, v in title.items() if k != "roles"}
            if group == "Actor":
                item["character"] = " / ".join(dict.fromkeys(r["character"] for r in roles if r["character"])) or None
                item["jobs"] = []
            else:
                item["character"] = None
                item["jobs"] = list(dict.fromkeys(r["job"] for r in roles))
            groups.setdefault((title["type"], group), []).append(item)
    ordered = sorted(groups.items(), key=lambda kv: (kv[0][0] != "movie", CREDIT_ORDER.index(kv[0][1])))
    return [{"kind": kind, "job": group, "total_credits": len(items), "credits": items}
            for (kind, group), items in ordered]

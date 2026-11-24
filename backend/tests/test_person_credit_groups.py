import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from core.person_credits import format_person_credits, group_person_credits
from routers import media


def credit(tid, kind="movie", **extra):
    return {"id": tid, "media_type": kind, "title": f"Title {tid}", "name": f"Show {tid}",
            "popularity": 100, "poster_path": None, "genre_ids": [18], **extra}


def crew(tid, job, department, kind="movie", **extra):
    return credit(tid, kind, job=job, department=department, **extra)


class FormatPersonCreditsTests(unittest.TestCase):
    def test_same_numeric_movie_and_tv_id_remain_distinct(self):
        result = format_person_credits({"cast": [credit(1), credit(1, "tv")]}, lambda p: p)
        self.assertEqual({(c["type"], c["tmdb_id"]) for c in result}, {("movie", 1), ("series", 1)})

    def test_every_job_of_a_title_survives_deduplication(self):
        data = {"cast": [credit(1, character="A")],
                "crew": [crew(1, "Director", "Directing"), crew(1, "Writer", "Writing"),
                         crew(1, "Director", "Directing")]}
        titles = format_person_credits(data, lambda p: p)
        self.assertEqual(len(titles), 1)
        self.assertEqual([(r["department"], r["job"]) for r in titles[0]["roles"]],
                         [("Acting", "Actor"), ("Directing", "Director"), ("Writing", "Writer")])

    def test_unsupported_or_missing_media_ids_are_skipped(self):
        titles = format_person_credits({"cast": [credit(1, "person"), credit(None), credit(0)]}, lambda p: p)
        self.assertEqual(titles, [])


class GroupPersonCreditsTests(unittest.TestCase):
    def test_groups_by_kind_then_department_with_movies_first(self):
        titles = format_person_credits({
            "cast": [credit(1, character="Hero"), credit(2, "tv", character="Cop")],
            "crew": [crew(3, "Director", "Directing"), crew(3, "Producer", "Production"),
                     crew(4, "Writer", "Writing", "tv")]}, lambda p: p)
        groups = group_person_credits(titles)
        self.assertEqual([(g["kind"], g["job"], g["total_credits"]) for g in groups],
                         [("movie", "Actor", 1), ("movie", "Directing", 1), ("movie", "Production", 1),
                          ("series", "Actor", 1), ("series", "Story", 1)])

    def test_title_with_several_departments_appears_in_each_group_with_that_departments_jobs(self):
        titles = format_person_credits({
            "cast": [credit(1, character="Hero")],
            "crew": [crew(1, "Director", "Directing"), crew(1, "Screenplay", "Writing"),
                     crew(1, "Writer", "Writing")]}, lambda p: p)
        groups = {g["job"]: g["credits"][0] for g in group_person_credits(titles)}
        self.assertEqual(set(groups), {"Actor", "Directing", "Story"})
        self.assertEqual(groups["Actor"]["character"], "Hero")
        self.assertEqual(groups["Directing"]["jobs"], ["Director"])
        self.assertEqual(groups["Story"]["jobs"], ["Screenplay", "Writer"])

    def test_key_jobs_come_first_then_the_rest_by_name(self):
        titles = format_person_credits({"crew": [
            crew(1, "Associate Producer", "Production"), crew(1, "Executive Producer", "Production"),
            crew(1, "Line Producer", "Production"), crew(1, "Producer", "Production"),
            crew(1, "Writer", "Writing"), crew(1, "Characters", "Writing"), crew(1, "Screenplay", "Writing"),
            crew(1, "Composer", "Sound"), crew(1, "Sound Mixer", "Sound"),
            crew(1, "Original Music Composer", "Sound")]}, lambda p: p)
        jobs = {g["job"]: g["credits"][0]["jobs"] for g in group_person_credits(titles)}
        self.assertEqual(jobs["Production"], ["Producer", "Executive Producer", "Associate Producer", "Line Producer"])
        self.assertEqual(jobs["Story"], ["Screenplay", "Writer", "Characters"])
        self.assertEqual(jobs["Music"], ["Original Music Composer", "Composer"])
        self.assertEqual(jobs["Other"], ["Sound Mixer"])

    def test_groups_follow_the_users_order_with_production_last(self):
        titles = format_person_credits({
            "cast": [credit(1, character="Hero")],
            "crew": [crew(1, "Producer", "Production"), crew(1, "Gaffer", "Lighting"),
                     crew(1, "Special Thanks", "Thanks"), crew(1, "Visual Effects Supervisor", "Visual Effects"),
                     crew(1, "Original Music Composer", "Sound"), crew(1, "Editor", "Editing"),
                     crew(1, "Director of Photography", "Camera"), crew(1, "Screenplay", "Writing"),
                     crew(1, "Director", "Directing")]}, lambda p: p)
        self.assertEqual([g["job"] for g in group_person_credits(titles)],
                         ["Actor", "Directing", "Story", "Camera", "Editing", "Music",
                          "Effects", "Other", "Thanks", "Production"])

    def test_unknown_or_missing_department_goes_to_other(self):
        titles = format_person_credits({"crew": [crew(1, "Gaffer", "Zebra"), credit(2, job="Gaffer")]},
                                       lambda p: p)
        self.assertEqual([(g["job"], g["total_credits"]) for g in group_person_credits(titles)], [("Other", 2)])

    def test_roles_of_a_title_list_the_best_job_first(self):
        titles = format_person_credits({"crew": [crew(1, "Associate Producer", "Production"),
                                                 crew(1, "Producer", "Production")]}, lambda p: p)
        self.assertEqual(group_person_credits(titles)[0]["credits"][0]["jobs"], ["Producer", "Associate Producer"])


class PersonGroupEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, source, **overrides):
        result = MagicMock()
        result.all.return_value = []
        db = AsyncMock()
        db.execute.return_value = result

        async def enrich(_db, _user, items):
            for item in items:
                item["in_library"] = item["tmdb_id"] == 1

        args = dict(person_id=99, page=1, collection=None, genre=[], year=[], min_rating=None,
                    grouped=True, credit_kind=None, credit_job=None, db=db,
                    current_user=SimpleNamespace(id=7))
        args.update(overrides)
        with patch.object(media, "get_user_tmdb_key", new=AsyncMock(return_value="synthetic-key")), \
             patch.object(media, "check_tmdb_key", return_value=True), \
             patch.object(media.tmdb, "get_person",
                          new=AsyncMock(return_value={"id": 99, "name": "Person", "combined_credits": source})), \
             patch.object(media, "enrich_with_state", new=AsyncMock(side_effect=enrich)):
            return await media.get_person_details(**args)

    async def test_default_request_keeps_flat_paginated_credits_and_no_groups(self):
        source = {"cast": [credit(i) for i in range(1, 31)]}
        result = await self.call(source, grouped=False, page=2)
        self.assertEqual(result["total_credits"], 30)
        self.assertEqual(len(result["credits"]), 10)
        self.assertEqual(result["credit_groups"], [])
        self.assertNotIn("roles", result["credits"][0])

    async def test_groups_use_full_credit_set_before_pagination(self):
        source = {"cast": [credit(i) for i in range(1, 31)], "crew": [crew(31, "Director", "Directing", "tv")]}
        result = await self.call(source)
        self.assertEqual(result["total_credits"], 31)
        self.assertEqual([(g["kind"], g["job"], g["total_credits"], len(g["credits"]))
                          for g in result["credit_groups"]],
                         [("movie", "Actor", 30, 12), ("series", "Directing", 1, 1)])
        self.assertEqual(result["available_jobs"], ["Actor", "Directing"])

    async def test_grouped_requests_use_a_smaller_page_size_that_the_partial_shares(self):
        result = await self.call({"cast": [credit(i) for i in range(1, 31)]})
        self.assertEqual(result["page_size"], 12)
        self.assertEqual(len(result["credit_groups"][0]["credits"]), 12)
        flat = await self.call({"cast": [credit(i) for i in range(1, 31)]}, grouped=False)
        self.assertEqual(flat["page_size"], 20)

    async def test_group_page_does_not_mix_kinds_or_jobs(self):
        source = {"cast": [credit(i) for i in range(1, 31)], "crew": [crew(1, "Director", "Directing")]}
        result = await self.call(source, grouped=False, credit_kind="movie", credit_job="Actor", page=2)
        self.assertEqual(result["total_credits"], 30)
        self.assertEqual([c["tmdb_id"] for c in result["credits"]], list(range(13, 25)))
        self.assertTrue(all(c["jobs"] == [] and "character" in c for c in result["credits"]))

    async def test_job_filter_narrows_groups_and_keeps_all_jobs_listed(self):
        source = {"cast": [credit(1, character="Hero")],
                  "crew": [crew(1, "Director", "Directing"), crew(2, "Writer", "Writing")]}
        result = await self.call(source, credit_job="Story")
        self.assertEqual([g["job"] for g in result["credit_groups"]], ["Story"])
        self.assertEqual(result["total_credits"], 1)
        self.assertEqual([c["tmdb_id"] for c in result["credits"]], [2])
        self.assertEqual(result["available_jobs"], ["Actor", "Directing", "Story"])

    async def test_collection_filter_is_applied_before_group_totals(self):
        source = {"cast": [credit(1), credit(2)], "crew": [crew(1, "Director", "Directing")]}
        result = await self.call(source, collection="in")
        self.assertEqual(result["total_credits"], 1)
        self.assertEqual([g["total_credits"] for g in result["credit_groups"]], [1, 1])

    async def test_genre_and_year_filters_apply_to_every_group(self):
        source = {"cast": [credit(1, release_date="2001-01-01"), credit(2, release_date="2010-01-01")],
                  "crew": [crew(3, "Director", "Directing", "tv", release_date="2001-01-01")]}
        result = await self.call(source, year=[2001], genre=["Drama"])
        self.assertEqual(result["total_credits"], 2)
        self.assertEqual([(g["kind"], g["job"]) for g in result["credit_groups"]],
                         [("movie", "Actor"), ("series", "Directing")])

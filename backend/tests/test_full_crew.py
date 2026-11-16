import os
import unittest

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")

from core.tmdb import full_crew


def row(pid, job, department, **extra):
    return {"id": pid, "name": f"Person {pid}", "job": job, "department": department, **extra}


class FullCrewTests(unittest.TestCase):
    def test_keeps_every_person_beyond_the_notable_card_limit(self):
        rows = [row(i, "Gaffer", "Lighting") for i in range(1, 31)]
        self.assertEqual(len(full_crew({"crew": rows})), 30)

    def test_person_with_several_jobs_is_one_entry_without_repeats(self):
        rows = [
            row(1, "Producer", "Production"),
            row(1, "Director", "Directing"),
            row(1, "Screenplay", "Writing"),
            row(1, "Director", "Directing"),
        ]
        crew = full_crew({"crew": rows})
        self.assertEqual(len(crew), 1)
        self.assertEqual(crew[0]["job"], "Director, Screenplay, Producer")

    def test_creative_departments_come_first_in_a_fixed_order(self):
        rows = [
            row(1, "Producer", "Production"),
            row(2, "Editor", "Editing"),
            row(3, "Original Music Composer", "Sound"),
            row(4, "Director of Photography", "Camera"),
            row(5, "Story", "Writing"),
            row(6, "Director", "Directing"),
            row(7, "Visual Effects Supervisor", "Visual Effects"),
            row(8, "Sound Designer", "Sound"),
            row(9, "Special Thanks", "Thanks"),
        ]
        order = [p["tmdb_id"] for p in full_crew({"crew": rows})]
        self.assertEqual(order, [6, 5, 4, 2, 3, 7, 8, 9, 1])

    def test_key_jobs_lead_their_department_before_alphabetical_order(self):
        rows = [
            {"id": 1, "name": "Aaron Adams", "job": "Third Assistant Director", "department": "Directing"},
            {"id": 2, "name": "Bella Brandt", "job": "Second Unit Director", "department": "Directing"},
            {"id": 3, "name": "Zed Zimmer", "job": "Director", "department": "Directing"},
            {"id": 4, "name": "Carl Cole", "job": "Co-Director", "department": "Directing"},
            {"id": 5, "name": "Ann Arden", "job": "Assistant Editor", "department": "Editing"},
            {"id": 6, "name": "Yan Young", "job": "Editor", "department": "Editing"},
            {"id": 7, "name": "Abe Ames", "job": "Associate Producer", "department": "Production"},
            {"id": 8, "name": "Walt West", "job": "Executive Producer", "department": "Production"},
            {"id": 9, "name": "Zoe Zane", "job": "Producer", "department": "Production"},
        ]
        order = [p["tmdb_id"] for p in full_crew({"crew": rows})]
        self.assertEqual(order, [3, 4, 2, 1, 6, 5, 9, 8, 7])

    def test_a_person_is_placed_by_their_best_role(self):
        rows = [
            {"id": 1, "name": "Aaron Adams", "job": "Script Supervisor", "department": "Directing"},
            {"id": 2, "name": "Zed Zimmer", "job": "Script Supervisor", "department": "Directing"},
            {"id": 2, "name": "Zed Zimmer", "job": "Director", "department": "Directing"},
        ]
        crew = full_crew({"crew": rows})
        self.assertEqual([p["tmdb_id"] for p in crew], [2, 1])
        self.assertEqual(crew[0]["job"], "Director, Script Supervisor")

    def test_key_job_synonyms_and_music_follow_the_shared_rule(self):
        rows = [
            {"id": 1, "name": "Ann Arden", "job": "Sound Mixer", "department": "Sound"},
            {"id": 2, "name": "Bo Berg", "job": "Cinematography", "department": "Camera"},
            {"id": 3, "name": "Cy Cole", "job": "Teleplay", "department": "Writing"},
            {"id": 4, "name": "Di Dunn", "job": "Songs", "department": "Sound"},
            {"id": 5, "name": "Ed Eck", "job": "Creator", "department": "Writing"},
        ]
        self.assertEqual([p["tmdb_id"] for p in full_crew({"crew": rows})], [5, 3, 2, 4, 1])

    def test_same_group_is_sorted_by_name(self):
        rows = [
            {"id": 2, "name": "Zoe Vance", "job": "Gaffer", "department": "Lighting"},
            {"id": 3, "name": "adam Brook", "job": "Grip", "department": "Crew"},
        ]
        self.assertEqual([p["tmdb_id"] for p in full_crew({"crew": rows})], [3, 2])

    def test_missing_or_invalid_rows_are_not_invented(self):
        self.assertEqual(full_crew(None), [])
        self.assertEqual(full_crew({}), [])
        self.assertEqual(full_crew({"crew": None}), [])
        rows = [
            row(None, "Writer", "Writing"),
            row(True, "Writer", "Writing"),
            row(0, "Writer", "Writing"),
            row(1, "Writer", "Writing", name=None),
            row(2, None, None, profile_path="/p.jpg"),
        ]
        crew = full_crew({"crew": rows})
        self.assertEqual(len(crew), 1)
        self.assertEqual(crew[0]["job"], "Crew")
        self.assertTrue(crew[0]["profile_path"].endswith("/p.jpg"))

    def test_profile_size_is_passed_through(self):
        crew = full_crew({"crew": [row(1, "Director", "Directing", profile_path="/p.jpg")]}, profile_size="w185")
        self.assertIn("/w185/p.jpg", crew[0]["profile_path"])


if __name__ == "__main__":
    unittest.main()

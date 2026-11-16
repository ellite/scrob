import unittest

from core.credit_order import (
    CREDIT_ORDER, is_key_role, key_rank, person_position, role_group, role_sort_key, sort_roles,
)


def role(job, department):
    return {'job': job, 'department': department}


class RoleGroupTests(unittest.TestCase):
    def test_sound_is_split_into_music_and_the_rest(self):
        self.assertEqual(role_group(role('Original Music Composer', 'Sound')), 'Music')
        self.assertEqual(role_group(role('Songs', 'Sound')), 'Music')
        self.assertEqual(role_group(role('Orchestrator', 'Sound')), 'Music')
        self.assertEqual(role_group(role('Sound Mixer', 'Sound')), 'Other')

    def test_departments_map_to_groups(self):
        self.assertEqual(role_group(role('Actor', 'Acting')), 'Actor')
        self.assertEqual(role_group(role('Screenplay', 'Writing')), 'Story')
        self.assertEqual(role_group(role('Creator', 'Creator')), 'Story')
        self.assertEqual(role_group(role('Gaffer', 'Lighting')), 'Other')
        self.assertEqual(role_group(role('Gaffer', None)), 'Other')
        self.assertEqual(role_group(role('Special Thanks', 'Crew')), 'Thanks')

    def test_group_order_is_the_users_rule_with_production_last(self):
        self.assertEqual(CREDIT_ORDER[1:6], ['Directing', 'Story', 'Camera', 'Editing', 'Music'])
        self.assertEqual(CREDIT_ORDER[-1], 'Production')


class KeyJobTests(unittest.TestCase):
    def test_synonyms_share_a_rank(self):
        self.assertEqual(key_rank(role('Screenplay', 'Writing')), key_rank(role('Writer', 'Writing')))
        self.assertTrue(is_key_role(role('Cinematography', 'Camera')))
        self.assertTrue(is_key_role(role('Teleplay', 'Writing')))
        self.assertTrue(is_key_role(role('Creator', 'Writing')))

    def test_key_jobs_lead_their_group_before_the_rest_by_name(self):
        jobs = ['Associate Producer', 'Executive Producer', 'Line Producer', 'Producer']
        ordered = sorted((role(j, 'Production') for j in jobs), key=role_sort_key)
        self.assertEqual([r['job'] for r in ordered],
                         ['Producer', 'Executive Producer', 'Associate Producer', 'Line Producer'])

    def test_creator_comes_before_screenplay(self):
        ordered = sort_roles([role('Writer', 'Writing'), role('Creator', 'Writing')])
        self.assertEqual([r['job'] for r in ordered], ['Creator', 'Writer'])


class ShowrunnerTests(unittest.TestCase):
    def test_showrunner_is_a_story_key_job_in_any_department(self):
        for department in ('Production', 'Writing', 'Creator', None):
            r = role('Showrunner', department)
            self.assertEqual(role_group(r), 'Story')
            self.assertTrue(is_key_role(r))
        self.assertEqual(role_group(role('showrunner', 'Production')), 'Story')

    def test_showrunner_ranks_between_creator_and_writer(self):
        ranks = [key_rank(role(j, 'Writing')) for j in ('Creator', 'Showrunner', 'Writer')]
        self.assertTrue(ranks[0] < ranks[1] < ranks[2])


class PersonPositionTests(unittest.TestCase):
    def test_best_role_decides_the_position_and_leads_the_role_list(self):
        roles = sort_roles([role('Gaffer', 'Lighting'), role('Producer', 'Production'),
                            role('Director', 'Directing')])
        self.assertEqual([r['job'] for r in roles], ['Director', 'Producer', 'Gaffer'])
        self.assertEqual(person_position(roles), (CREDIT_ORDER.index('Directing'), 0))

    def test_a_key_job_beats_an_earlier_group_without_one(self):
        roles = sort_roles([role('Script Supervisor', 'Directing'), role('Producer', 'Production')])
        self.assertEqual(roles[0]['job'], 'Producer')
        self.assertEqual(person_position(roles), (CREDIT_ORDER.index('Production'), 0))

    def test_without_key_jobs_the_first_role_by_group_wins(self):
        roles = sort_roles([role('Gaffer', 'Lighting'), role('Script Supervisor', 'Directing')])
        self.assertEqual(roles[0]['job'], 'Script Supervisor')


if __name__ == '__main__':
    unittest.main()

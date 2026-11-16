"""One ordering rule for film/TV credits, shared by title pages and person pages.

A credit ("role") is a dict with a TMDB `job` and `department`. Roles are sorted into
display groups (directing first, production last), and inside a group the key jobs
(director, screenplay, ...) lead before everything else by name.
"""

# Display order of the credit groups: the creative departments a viewer looks for first,
# then the rest, production last. "Actor" only occurs on person pages.
CREDIT_ORDER = ['Actor', 'Directing', 'Story', 'Camera', 'Editing', 'Music', 'Effects', 'Other', 'Thanks', 'Production']

# Key jobs lead their group in this order; names inside one tuple are synonyms that share a
# rank. Everything that is not listed follows by name.
KEY_JOBS = {
    'Directing': [('director',), ('co-director',), ('second unit director',)],
    'Story': [('creator',), ('showrunner',), ('screenplay', 'writer'), ('teleplay',), ('story',), ('novel',), ('characters',)],
    'Camera': [('director of photography', 'cinematography')],
    'Editing': [('editor',)],
    'Music': [('original music composer',), ('music',), ('composer',)],
    'Production': [('producer',), ('executive producer',)],
}

_MUSIC_WORDS = ('music', 'composer', 'song', 'score', 'orchestr')


def role_group(role):
    """The display group of a role. TMDB "Sound" is split: music jobs are Music, the rest Other."""
    job = (role.get('job') or '').casefold().strip()
    department = (role.get('department') or '').casefold().strip()
    if job == 'actor':
        return 'Actor'
    if department == 'thanks' or job in ('thanks', 'special thanks', 'thanked'):
        return 'Thanks'
    # The showrunner leads a series' writing whatever department TMDB/TVDB files it under.
    if job == 'showrunner':
        return 'Story'
    if department == 'sound':
        return 'Music' if any(word in job for word in _MUSIC_WORDS) else 'Other'
    return {'directing': 'Directing', 'camera': 'Camera', 'writing': 'Story', 'creator': 'Story',
            'editing': 'Editing', 'visual effects': 'Effects', 'production': 'Production'}.get(department, 'Other')


def key_rank(role):
    """Position of a role among its group's key jobs; len(all) when it is not a key job."""
    jobs = KEY_JOBS.get(role_group(role), [])
    job = (role.get('job') or '').casefold().strip()
    return next((i for i, names in enumerate(jobs) if job in names), len(jobs))


def is_key_role(role):
    group = role_group(role)
    return group in KEY_JOBS and key_rank(role) < len(KEY_JOBS[group])


def role_sort_key(role):
    """Order of roles: group, then key rank, then job name."""
    return (CREDIT_ORDER.index(role_group(role)), key_rank(role), (role.get('job') or '').casefold())


def person_role_sort_key(role):
    """A person's roles: key jobs first (group order, then rank), the rest after them."""
    return (0 if is_key_role(role) else 1,) + role_sort_key(role)


def sort_roles(roles):
    """A person's roles, best role first."""
    return sorted(roles, key=person_role_sort_key)


def person_position(roles):
    """Where a person stands in a full list: the position of their best role. `roles` must
    already be sorted with sort_roles."""
    return role_sort_key(roles[0])[:2]

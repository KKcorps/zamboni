import amo
from apps.search.views import _get_locale_analyzer

import mkt

from . import forms


DEFAULT_FILTERS = ['cat', 'device', 'form_factor', 'platform', 'premium_types',
                   'price', 'sort']
DEFAULT_SORTING = {
    'popularity': '-popularity',
    # TODO: Should popularity replace downloads?
    'downloads': '-weekly_downloads',
    'rating': '-bayesian_rating',
    'created': '-created',
    'reviewed': '-reviewed',
    'name': 'name_sort',
}


def get_custom_analyzer(language):
    """
    Returns name of analyzer based on language name.
    """
    if language in amo.STEMMER_MAP:
        return '%s_analyzer' % language
    return language


def name_only_query(q):
    """
    Returns a dictionary with field/value mappings to pass to elasticsearch.

    This sets up various queries with boosting against the name field in the
    elasticsearch index.

    """
    d = {}

    rules = {
        'match': {'query': q, 'boost': 3, 'analyzer': 'standard'},
        'match': {'query': q, 'boost': 4, 'type': 'phrase', 'slop': 1},
        'startswith': {'value': q, 'boost': 1.5}
    }

    # Only add fuzzy queries if q is a single word. It doesn't make sense to do
    # a fuzzy query for multi-word queries.
    if ' ' not in q:
        rules['fuzzy'] = {'value': q, 'boost': 2, 'prefix_length': 1}

    for k, v in rules.iteritems():
        for field in ('name', 'app_slug', 'author'):
            d['%s__%s' % (field, k)] = v

    # Exact matches need to be queried against a non-analyzed field. Let's do a
    # term query on `name_sort` for an exact match against the app name and
    # give it a good boost since this is likely what the user wants.
    d['name_sort__term'] = {'value': q, 'boost': 10}

    analyzer = _get_locale_analyzer()
    if analyzer:
        d['name_%s__match' % analyzer] = {
            'query': q, 'boost': 2.5,
            'analyzer': get_custom_analyzer(analyzer)}
    return d


def name_query(q):
    """
    Returns a dictionary with field/value mappings to pass to elasticsearch.

    Note: This is marketplace specific. See apps/search/views.py for AMO.

    """
    more = {
        'description__match': {'query': q, 'boost': 0.8, 'type': 'phrase'},
    }

    analyzer = _get_locale_analyzer()
    if analyzer:
        more['description_%s__match' % analyzer] = {
            'query': q, 'boost': 0.6, 'type': 'phrase',
            'analyzer': get_custom_analyzer(analyzer)}

    more['tags__match'] = {'query': q}
    if ' ' not in q:
        more['tags__fuzzy'] = {'value': q, 'prefix_length': 1}

    return dict(more, **name_only_query(q))


def _filter_search(request, qs, query, filters=None, sorting=None,
                   sorting_default='-popularity', region=None, profile=None):
    """
    Filter an ES queryset based on a list of filters.

    If `profile` (a FeatureProfile object) is provided we filter by the
    profile. If you don't want to filter by these don't pass it. I.e. do the
    device detection for when this happens elsewhere.

    """
    # Intersection of the form fields present and the filters we want to apply.
    filters = filters or DEFAULT_FILTERS
    sorting = sorting or DEFAULT_SORTING
    show = filter(query.get, filters)

    if query.get('q'):
        qs = qs.query(should=True, **name_query(query['q'].lower()))
    if 'cat' in show:
        qs = qs.filter(category=query['cat'])
    if 'price' in show:
        if query['price'] == 'paid':
            qs = qs.filter(premium_type__in=amo.ADDON_PREMIUMS)
        elif query['price'] == 'free':
            qs = qs.filter(premium_type__in=amo.ADDON_FREES, price=0)
    if 'platform' in show and query['platform'] in mkt.PLATFORM_LOOKUP:
        qs = qs.filter(platforms=mkt.PLATFORM_LOOKUP[query['platform']].id)
    # 'device' is here to maintain compatibility with API v1.
    # TODO: Switch to platform/form_factor query.
    if 'device' in show and query['device'] in forms.DEVICE_CHOICES_IDS:
        qs = qs.filter(device=forms.DEVICE_CHOICES_IDS[query['device']])
    if 'form_factor' in show and query['form_factor'] in mkt.FORM_FACTOR_LOOKUP:
        qs = qs.filter(
            form_factors=mkt.FORM_FACTOR_LOOKUP[query['form_factor']].id)
    if 'premium_types' in show:
        if query.get('premium_types'):
            qs = qs.filter(premium_type__in=query['premium_types'])
    if query.get('app_type'):
        # Also include `privileged` apps even when we search for `packaged`.
        if 'packaged' in query['app_type']:
            query['app_type'].push('privileged')
        qs = qs.filter(app_type__in=query['app_type'])
    if query.get('manifest_url'):
        qs = qs.filter(manifest_url=query['manifest_url'])
    if query.get('offline') is not None:
        qs = qs.filter(is_offline=query.get('offline'))
    if query.get('languages'):
        langs = [x.strip() for x in query['languages'].split(',')]
        qs = qs.filter(supported_locales__in=langs)
    if 'sort' in show:
        sort_by = [sorting[name] for name in query['sort'] if name in sorting]

        # For "Adolescent" regions popularity is global installs + reviews.

        if query['sort'] == 'popularity' and region and not region.adolescent:
            # For "Mature" regions popularity becomes installs + reviews
            # from only that region.
            sort_by = ['-popularity_%s' % region.id]

        if sort_by:
            qs = qs.order_by(*sort_by)
    elif not query.get('q'):

        if (sorting_default == 'popularity' and region and
            not region.adolescent):
            # For "Mature" regions popularity becomes installs + reviews
            # from only that region.
            sorting_default = '-popularity_%s' % region.id

        # Sort by a default if there was no query so results are predictable.
        qs = qs.order_by(sorting_default)

    if profile:
        # Exclude apps that require any features we don't support.
        qs = qs.filter(**profile.to_kwargs(prefix='features.has_'))

    return qs

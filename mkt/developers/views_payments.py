import json
import urllib

from django import http
from django.conf import settings
from django.shortcuts import get_object_or_404, redirect, render

import commonware
import jinja2
import waffle

from curling.lib import HttpClientError
from tower import ugettext as _
from waffle.decorators import waffle_switch

import amo
from access import acl
from amo import messages
from amo.decorators import json_view, login_required, post_required, write
from amo.urlresolvers import reverse
from constants.payments import (PAYMENT_METHOD_ALL, PAYMENT_METHOD_CARD,
                                PAYMENT_METHOD_OPERATOR, PROVIDER_BANGO,
                                PROVIDER_CHOICES)
from lib.crypto import generate_key
from lib.pay_server import client
from market.models import Price

import mkt
from mkt.developers import forms, forms_payments
from mkt.developers.decorators import dev_required
from mkt.developers.models import CantCancel, PaymentAccount, UserInappKey
from mkt.developers.providers import get_provider, get_providers
from mkt.developers.utils import uri_to_pk
from mkt.inapp.models import InAppProduct
from mkt.inapp.serializers import InAppProductForm
from mkt.webapps.models import Webapp


log = commonware.log.getLogger('z.devhub')


@dev_required
@post_required
def disable_payments(request, addon_id, addon):
    addon.update(wants_contributions=False)
    return redirect(addon.get_dev_url('payments'))


@dev_required(owner_for_post=True, webapp=True)
def payments(request, addon_id, addon, webapp=False):
    premium_form = forms_payments.PremiumForm(
        request.POST or None, request=request, addon=addon,
        user=request.amo_user)

    region_form = forms.RegionForm(
        request.POST or None, product=addon, request=request)

    upsell_form = forms_payments.UpsellForm(
        request.POST or None, addon=addon, user=request.amo_user)

    providers = get_providers()

    if 'form-TOTAL_FORMS' in request.POST:
        formset_data = request.POST
    else:
        formset_data = None
    account_list_formset = forms_payments.AccountListFormSet(
        data=formset_data,
        provider_data=[
            {'addon': addon, 'user': request.amo_user, 'provider': provider}
            for provider in providers])

    if request.method == 'POST':

        active_forms = [premium_form, region_form, upsell_form]
        if formset_data is not None:
            active_forms.append(account_list_formset)

        success = all(form.is_valid() for form in active_forms)

        if success:
            region_form.save()

            try:
                premium_form.save()
            except client.Error as err:
                success = False
                log.error('Error setting payment information (%s)' % err)
                messages.error(
                    request, _(u'We encountered a problem connecting to the '
                               u'payment server.'))
                raise  # We want to see these exceptions!

            is_free_inapp = addon.premium_type == amo.ADDON_FREE_INAPP
            is_now_paid = (addon.premium_type in amo.ADDON_PREMIUMS
                           or is_free_inapp)

            # If we haven't changed to a free app, check the upsell.
            if is_now_paid and success:
                try:
                    if not is_free_inapp:
                        upsell_form.save()
                    if formset_data is not None:
                        account_list_formset.save()
                except client.Error as err:
                    log.error('Error saving payment information (%s)' % err)
                    messages.error(
                        request, _(u'We encountered a problem connecting to '
                                   u'the payment server.'))
                    success = False
                    raise  # We want to see all the solitude errors now.

        # If everything happened successfully, give the user a pat on the back.
        if success:
            messages.success(request, _('Changes successfully saved.'))
            return redirect(addon.get_dev_url('payments'))

    # TODO: refactor this (bug 945267)
    is_packaged = addon.is_packaged
    android_payments_enabled = waffle.flag_is_active(request,
                                                     'android-payments')
    android_packaged_enabled = waffle.flag_is_active(request,
                                                     'android-packaged')
    desktop_packaged_enabled = waffle.flag_is_active(request,
                                                     'desktop-packaged')

    # If android payments is not allowed then firefox os must be 'checked' and
    # android should not be.
    invalid_paid_platform_state = []

    # FirefoxOS payments are ok.
    invalid_paid_platform_state.append(('firefoxos', False))

    # We don't support desktop payments anywhere yet.
    invalid_paid_platform_state.append(('desktop', True))

    if not android_payments_enabled:
        # When android-payments is off...
        # If not packaged or it is packaged and the android-packaged flag is
        # on then we should check for the state of android.
        if not is_packaged or (is_packaged and android_packaged_enabled):
            invalid_paid_platform_state += [('android', True)]

    cannot_be_paid = (
        addon.premium_type == amo.ADDON_FREE and
        any(premium_form.platform_data['free-%s' % x] == y
            for x, y in invalid_paid_platform_state))

    try:
        tier_zero = Price.objects.get(price='0.00', active=True)
        tier_zero_id = tier_zero.pk
    except Price.DoesNotExist:
        tier_zero = None
        tier_zero_id = ''

    # Get the regions based on tier zero. This should be all the
    # regions with payments enabled.
    paid_region_ids_by_slug = []
    if tier_zero:
        paid_region_ids_by_slug = tier_zero.region_ids_by_slug()

    return render(request, 'developers/payments/premium.html',
                  {'addon': addon, 'webapp': webapp, 'premium': addon.premium,
                   'form': premium_form, 'upsell_form': upsell_form,
                   'free_forms': mkt.FREE_FORMS,
                   'paid_forms': mkt.PAID_FORMS,
                   'tier_zero_id': tier_zero_id, 'region_form': region_form,
                   'is_paid': (addon.premium_type in amo.ADDON_PREMIUMS or
                               addon.premium_type == amo.ADDON_FREE_INAPP),
                   'cannot_be_paid': cannot_be_paid,
                   'platforms': [p.slug for p in addon.platforms],
                   'has_incomplete_status': addon.status == amo.STATUS_NULL,
                   'is_packaged': addon.is_packaged,
                   # Bango values
                   'account_list_forms': account_list_formset.forms,
                   'account_list_formset': account_list_formset,
                   # Waffles
                   'api_pricelist_url': reverse('price-list'),
                   'payment_methods': {
                       PAYMENT_METHOD_ALL: _('All'),
                       PAYMENT_METHOD_CARD: _('Credit card'),
                       PAYMENT_METHOD_OPERATOR: _('Carrier'),
                   },
                   'provider_lookup': dict(PROVIDER_CHOICES),
                   'all_paid_region_ids_by_slug': paid_region_ids_by_slug,
                   'providers': providers})


@login_required
@json_view
def payment_accounts(request):
    app_slug = request.GET.get('app-slug', '')
    accounts = PaymentAccount.objects.filter(
        user=request.amo_user,
        provider__in=[p.provider for p in get_providers()],
        inactive=False)

    def account(acc):
        app_names = (', '.join(unicode(apa.addon.name)
                     for apa in acc.addonpaymentaccount_set.all()
                        if hasattr(apa, 'addon')))
        provider = acc.get_provider()
        data = {
            'account-url':
                reverse('mkt.developers.provider.payment_account',
                        args=[acc.pk]),
            'agreement-url': acc.get_agreement_url(),
            'agreement': 'accepted' if acc.agreed_tos else 'rejected',
            'app-names': jinja2.escape(app_names),
            'delete-url':
                reverse('mkt.developers.provider.delete_payment_account',
                        args=[acc.pk]),
            'id': acc.pk,
            'name': jinja2.escape(unicode(acc)),
            'provider': provider.name,
            'provider-full': unicode(provider.full),
            'shared': acc.shared,
            'portal-url': provider.get_portal_url(app_slug)
        }
        return data

    return map(account, accounts)


@login_required
def payment_accounts_form(request):
    webapp = get_object_or_404(Webapp, app_slug=request.GET.get('app_slug'))
    provider = get_provider(name=request.GET.get('provider'))
    account_list_formset = forms_payments.AccountListFormSet(
        provider_data=[
            {'user': request.amo_user, 'addon': webapp, 'provider': p}
            for p in get_providers()])
    account_list_form = next(form for form in account_list_formset.forms
                             if form.provider.name == provider.name)
    return render(request,
                  'developers/payments/includes/bango_accounts_form.html',
                  {'account_list_form': account_list_form})


@write
@post_required
@login_required
@json_view
def payments_accounts_add(request):
    provider = get_provider(name=request.POST.get('provider'))
    form = provider.forms['account'](request.POST)
    if not form.is_valid():
        return json_view.error(form.errors)

    try:
        obj = provider.account_create(request.amo_user, form.cleaned_data)
    except HttpClientError as e:
        log.error('Client error create {0} account: {1}'.format(
            provider.name, e))
        return http.HttpResponseBadRequest(json.dumps(e.content))

    return {'pk': obj.pk, 'agreement-url': obj.get_agreement_url()}


@write
@login_required
@json_view
def payments_account(request, id):
    account = get_object_or_404(PaymentAccount, pk=id, user=request.user)
    provider = account.get_provider()
    if request.POST:
        form = provider.forms['account'](request.POST, account=account)
        if form.is_valid():
            form.save()
        else:
            return json_view.error(form.errors)

    return provider.account_retrieve(account)


@write
@post_required
@login_required
def payments_accounts_delete(request, id):
    account = get_object_or_404(PaymentAccount, pk=id, user=request.user)
    try:
        account.cancel(disable_refs=True)
    except CantCancel:
        log.info('Could not cancel account.')
        return http.HttpResponse('Cannot cancel account', status=409)

    log.info('Account cancelled: %s' % id)
    return http.HttpResponse('success')


@login_required
def in_app_keys(request):
    keys = UserInappKey.objects.no_cache().filter(
        solitude_seller__user=request.amo_user
    )

    # TODO(Kumar) support multiple test keys. For now there's only one.
    key = None
    key_public_id = None

    if keys.exists():
        key = keys.get()

        # Attempt to retrieve the public id from solitude
        try:
            key_public_id = key.public_id()
        except HttpClientError, e:
            messages.error(request,
                           _('A server error occurred '
                             'when retrieving the application key.'))
            log.exception('Solitude connection error: {0}'.format(e.message))

    if request.method == 'POST':
        if key:
            key.reset()
            messages.success(request, _('Secret was reset successfully.'))
        else:
            UserInappKey.create(request.amo_user)
            messages.success(request,
                             _('Key and secret were created successfully.'))
        return redirect(reverse('mkt.developers.apps.in_app_keys'))

    return render(request, 'developers/payments/in-app-keys.html',
                  {'key': key, 'key_public_id': key_public_id})


@login_required
def in_app_key_secret(request, pk):
    key = (UserInappKey.objects.no_cache()
           .filter(solitude_seller__user=request.amo_user, pk=pk))
    if not key.count():
        # Either the record does not exist or it's not owned by the
        # logged in user.
        return http.HttpResponseForbidden()
    return http.HttpResponse(key.get().secret())


def require_in_app_payments(render_view):
    def inner(request, addon_id, addon, *args, **kwargs):
        inapp = addon.premium_type in amo.ADDON_INAPPS
        if not inapp:
            messages.error(
                    request,
                    _('Your app is not configured for in-app payments.'))
            return redirect(reverse('mkt.developers.apps.payments',
                                    args=[addon.app_slug]))
        else:
            return render_view(request, addon_id, addon, *args, **kwargs)
    return inner


@waffle_switch('in-app-products')
@login_required
@dev_required(webapp=True)
@require_in_app_payments
def in_app_products(request, addon_id, addon, webapp=True, account=None):
    owner = acl.check_addon_ownership(request, addon)
    products = addon.inappproduct_set.all()
    new_product = InAppProduct(webapp=addon)
    form = InAppProductForm()
    return render(request, 'developers/payments/in-app-products.html',
                  {'addon': addon, 'form': form, 'new_product': new_product,
                   'owner': owner, 'products': products, 'form': form})


@login_required
@dev_required(owner_for_post=True, webapp=True)
@require_in_app_payments
def in_app_config(request, addon_id, addon, webapp=True):
    if not addon.has_payment_account():
        messages.error(request, _('No payment account for this app.'))
        return redirect(reverse('mkt.developers.apps.payments',
                                args=[addon.app_slug]))

    # TODO: support multiple accounts.
    account = addon.single_pay_account()
    seller_config = get_seller_product(account)

    owner = acl.check_addon_ownership(request, addon)
    if request.method == 'POST':
        # Reset the in-app secret for the app.
        (client.api.generic
               .product(seller_config['resource_pk'])
               .patch(data={'secret': generate_key(48)}))
        messages.success(request, _('Changes successfully saved.'))
        return redirect(reverse('mkt.developers.apps.in_app_config',
                                args=[addon.app_slug]))

    return render(request, 'developers/payments/in-app-config.html',
                  {'addon': addon, 'owner': owner,
                   'seller_config': seller_config})


@login_required
@dev_required(webapp=True)
def in_app_secret(request, addon_id, addon, webapp=True):
    seller_config = get_seller_product(addon.single_pay_account())
    return http.HttpResponse(seller_config['secret'])


@dev_required(webapp=True)
def bango_portal_from_addon(request, addon_id, addon, webapp=True):
    try:
        bango = addon.payment_account(PROVIDER_BANGO)
    except addon.PayAccountDoesNotExist:
        log.error('Bango portal not available for app {app} '
                  'with accounts {acct}'
                  .format(app=addon,
                          acct=list(addon.all_payment_accounts())))
        return http.HttpResponseForbidden()
    else:
        account = bango.payment_account

    if not ((addon.authors.filter(user=request.user,
                addonuser__role=amo.AUTHOR_ROLE_OWNER).exists()) and
            (account.solitude_seller.user.id == request.user.id)):
        log.error(('User not allowed to reach the Bango portal; '
                   'pk=%s') % request.user.pk)
        return http.HttpResponseForbidden()

    return _redirect_to_bango_portal(account.account_id,
                                     'addon_id: %s' % addon_id)


def _redirect_to_bango_portal(package_id, source):
    try:
        bango_token = client.api.bango.login.post({'packageId':
                                                   int(package_id)})
    except HttpClientError as e:
        log.error('Failed to authenticate against Bango portal; %s' % source,
                  exc_info=True)
        return http.HttpResponseBadRequest(json.dumps(e.content))

    bango_url = '{base_url}{parameters}'.format(**{
        'base_url': settings.BANGO_BASE_PORTAL_URL,
        'parameters': urllib.urlencode({
            'authenticationToken': bango_token['authentication_token'],
            'emailAddress': bango_token['email_address'],
            'packageId': package_id,
            'personId': bango_token['person_id'],
        })
    })
    response = http.HttpResponse(status=204)
    response['Location'] = bango_url
    return response


def get_seller_product(account):
    """
    Get the solitude seller_product for a payment account object.
    """
    bango_product = (client.api.bango
                           .product(uri_to_pk(account.product_uri))
                           .get_object_or_404())
    # TODO(Kumar): we can optimize this by storing the seller_product
    # when we create it in developers/models.py or allowing solitude
    # to filter on both fields.
    return (client.api.generic
                  .product(uri_to_pk(bango_product['seller_product']))
                  .get_object_or_404())


# TODO(andym): move these into a DRF API.
@login_required
@json_view
def agreement(request, id):
    account = get_object_or_404(PaymentAccount, pk=id, user=request.user)
    provider = account.get_provider()
    if request.method == 'POST':
        return provider.terms_update(account)

    return provider.terms_retrieve(account)

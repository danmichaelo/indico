# This file is part of Indico.
# Copyright (C) 2002 - 2016 European Organization for Nuclear Research (CERN).
#
# Indico is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 3 of the
# License, or (at your option) any later version.
#
# Indico is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Indico; if not, see <http://www.gnu.org/licenses/>.

from __future__ import unicode_literals

from collections import OrderedDict

from flask import current_app, session, request
from sqlalchemy.orm import load_only, joinedload
from werkzeug.urls import url_parse
from wtforms import BooleanField, ValidationError

from indico.core.config import Config
from indico.core.db import db
from indico.modules.events import EventLogKind, EventLogRealm
from indico.modules.events.models.events import Event
from indico.modules.events.registration import logger
from indico.modules.events.registration.fields.choices import (ChoiceBaseField, AccommodationField,
                                                               get_field_merged_options)
from indico.modules.events.registration.models.form_fields import (RegistrationFormPersonalDataField,
                                                                   RegistrationFormFieldData)
from indico.modules.events.registration.models.forms import RegistrationForm
from indico.modules.events.registration.models.invitations import RegistrationInvitation, InvitationState
from indico.modules.events.registration.models.items import (RegistrationFormPersonalDataSection,
                                                             RegistrationFormItemType, PersonalDataType,
                                                             RegistrationFormItem)
from indico.modules.events.registration.models.registrations import Registration, RegistrationData, RegistrationState
from indico.modules.events.registration.notifications import (notify_registration_creation,
                                                              notify_registration_modification)
from indico.modules.events.util import ListGeneratorBase
from indico.modules.users.util import get_user_by_email
from indico.util.date_time import format_datetime, format_date
from indico.util.i18n import _
from indico.util.spreadsheets import unique_col
from indico.util.string import to_unicode
from indico.web.flask.templating import get_template_module
from indico.web.forms.base import IndicoForm
from indico.web.forms.widgets import SwitchWidget


def get_title_uuid(regform, title):
    """Convert a string title to its UUID value

    If the title does not exist in the title PD field, it will be
    ignored and returned as ``None``.
    """
    if not title:
        return None
    title_field = next((x
                        for x in regform.active_fields
                        if (x.type == RegistrationFormItemType.field_pd and
                            x.personal_data_type == PersonalDataType.title)), None)
    if title_field is None:  # should never happen
        return None
    valid_choices = {x['id'] for x in title_field.current_data.versioned_data['choices']}
    uuid = next((k for k, v in title_field.data['captions'].iteritems() if v == title), None)
    return {uuid: 1} if uuid in valid_choices else None


def user_registered_in_event(user, event):
    """
    Check whether there is a `Registration` entry for a user in any
    form tied to a particular event.

    :param user: the `User` object
    :param event: the event in question
    """
    return bool(Registration
                .find(Registration.user == user,
                      RegistrationForm.event_id == int(event.id),
                      ~RegistrationForm.is_deleted,
                      Registration.is_active)
                .join(Registration.registration_form)
                .count())


def get_event_section_data(regform, management=False, registration=None):
    data = []
    if not registration:
        return [s.view_data for s in regform.sections if not s.is_deleted and (management or not s.is_manager_only)]

    registration_data = {r.field_data.field.id: r for r in registration.data}
    for section in regform.sections:
        if section.is_deleted or (not management and section.is_manager_only):
            continue

        section_data = section.own_data
        section_data['items'] = []

        for child in section.fields:
            if child.is_deleted:
                continue
            if isinstance(child.field_impl, ChoiceBaseField) or isinstance(child.field_impl, AccommodationField):
                field_data = get_field_merged_options(child, registration_data)
            else:
                field_data = child.view_data
            section_data['items'].append(field_data)
        data.append(section_data)
    return data


def check_registration_email(regform, email, registration=None, management=False):
    """Checks whether an email address is suitable for registration.

    :param regform: The registration form
    :param email: The email address
    :param registration: The existing registration (in case of
                         modification)
    :param management: If it's a manager adding a new registration
    """
    email = email.lower().strip()
    user = get_user_by_email(email)
    email_registration = regform.get_registration(email=email)
    user_registration = regform.get_registration(user=user) if user else None
    if registration is not None:
        if email_registration and email_registration != registration:
            return dict(status='error', conflict='email-already-registered')
        elif user_registration and user_registration != registration:
            return dict(status='error', conflict='user-already-registered')
        elif user and registration.user and registration.user != user:
            return dict(status='warning' if management else 'error', conflict='email-other-user', user=user.full_name)
        elif not user and registration.user:
            return dict(status='warning' if management else 'error', conflict='email-no-user',
                        user=registration.user.full_name)
        elif user:
            return dict(status='ok', user=user.full_name, self=(not management and user == session.user),
                        same=(user == registration.user))
        elif regform.require_user and (management or email != registration.email):
            return dict(status='warning' if management else 'error', conflict='no-user')
        else:
            return dict(status='ok', user=None)
    else:
        if email_registration:
            return dict(status='error', conflict='email-already-registered')
        elif user_registration:
            return dict(status='error', conflict='user-already-registered')
        elif user:
            return dict(status='ok', user=user.full_name, self=(not management and user == session.user), same=False)
        elif regform.require_user:
            return dict(status='warning' if management else 'error', conflict='no-user')
        else:
            return dict(status='ok', user=None)


def make_registration_form(regform, management=False, registration=None):
    """Creates a WTForm based on registration form fields"""

    class RegistrationFormWTF(IndicoForm):
        if management:
            notify_user = BooleanField(_("Send email"), widget=SwitchWidget())

        def validate_email(self, field):
            status = check_registration_email(regform, field.data, registration, management=management)
            if status['status'] == 'error':
                raise ValidationError('Email validation failed: ' + status['conflict'])

    for form_item in regform.active_fields:
        if not management and form_item.parent.is_manager_only:
            continue
        field_impl = form_item.field_impl
        setattr(RegistrationFormWTF, form_item.html_field_name, field_impl.create_wtf_field())
    RegistrationFormWTF.modified_registration = registration
    return RegistrationFormWTF


def create_personal_data_fields(regform):
    """Creates the special section/fields for personal data."""
    section = next((s for s in regform.sections if s.type == RegistrationFormItemType.section_pd), None)
    if section is None:
        section = RegistrationFormPersonalDataSection(registration_form=regform, title='Personal Data')
        missing = set(PersonalDataType)
    else:
        existing = {x.personal_data_type for x in section.children if x.type == RegistrationFormItemType.field_pd}
        missing = set(PersonalDataType) - existing
    for pd_type, data in PersonalDataType.FIELD_DATA:
        if pd_type not in missing:
            continue
        field = RegistrationFormPersonalDataField(registration_form=regform, personal_data_type=pd_type,
                                                  is_required=pd_type.is_required)
        for key, value in data.iteritems():
            setattr(field, key, value)
        field.data, versioned_data = field.field_impl.process_field_data(data.pop('data', {}))
        field.current_data = RegistrationFormFieldData(versioned_data=versioned_data)
        section.children.append(field)


def url_rule_to_angular(endpoint):
    """Converts a flask-style rule to angular style"""
    mapping = {
        'reg_form_id': 'confFormId',
        'section_id': 'sectionId',
        'field_id': 'fieldId',
    }
    rules = list(current_app.url_map.iter_rules(endpoint))
    assert len(rules) == 1
    rule = rules[0]
    assert not rule.defaults
    segments = [':' + mapping.get(data, data) if is_dynamic else data
                for is_dynamic, data in rule._trace]
    prefix = url_parse(Config.getInstance().getBaseURL()).path.rstrip('/')
    return prefix + ''.join(segments).split('|', 1)[-1]


def create_registration(regform, data, invitation=None, management=False, notify_user=True):
    registration = Registration(registration_form=regform, user=get_user_by_email(data['email']),
                                base_price=regform.base_price, currency=regform.currency)
    for form_item in regform.active_fields:
        if form_item.parent.is_manager_only:
            with db.session.no_autoflush:
                value = form_item.field_impl.default_value
        else:
            value = data.get(form_item.html_field_name)
        with db.session.no_autoflush:
            data_entry = RegistrationData()
            registration.data.append(data_entry)
            for attr, value in form_item.field_impl.process_form_data(registration, value).iteritems():
                setattr(data_entry, attr, value)
        if form_item.type == RegistrationFormItemType.field_pd and form_item.personal_data_type.column:
            setattr(registration, form_item.personal_data_type.column, value)
    if invitation is None:
        # Associate invitation based on email in case the user did not use the link
        with db.session.no_autoflush:
            invitation = (RegistrationInvitation
                          .find(email=data['email'], registration_id=None)
                          .with_parent(regform)
                          .first())
    if invitation:
        invitation.state = InvitationState.accepted
        invitation.registration = registration
    registration.sync_state(_skip_moderation=management)
    db.session.flush()
    notify_registration_creation(registration, notify_user)
    logger.info('New registration %s by %s', registration, session.user)
    regform.event_new.log(EventLogRealm.management if management else EventLogRealm.participants,
                          EventLogKind.positive, 'Registration',
                          'New registration: {}'.format(registration.full_name),
                          session.user, data={'Email': registration.email})
    return registration


def modify_registration(registration, data, management=False, notify_user=True):
    old_price = registration.price
    with db.session.no_autoflush:
        regform = registration.registration_form
        data_by_field = registration.data_by_field
        if management or not registration.user:
            registration.user = get_user_by_email(data['email'])

        billable_items_locked = not management and registration.is_paid
        for form_item in regform.active_fields:
            field_impl = form_item.field_impl
            if management or not form_item.parent.is_manager_only:
                value = data.get(form_item.html_field_name)
            elif form_item.id not in data_by_field:
                # set default value for manager-only field if it didn't have one before
                value = field_impl.default_value
            else:
                # manager-only field that has data which should be preserved
                continue

            if form_item.id not in data_by_field:
                data_by_field[form_item.id] = RegistrationData(registration=registration,
                                                               field_data=form_item.current_data)

            attrs = field_impl.process_form_data(registration, value, data_by_field[form_item.id],
                                                 billable_items_locked=billable_items_locked)
            for key, val in attrs.iteritems():
                setattr(data_by_field[form_item.id], key, val)
            if form_item.type == RegistrationFormItemType.field_pd and form_item.personal_data_type.column:
                setattr(registration, form_item.personal_data_type.column, value)
        registration.sync_state()
    db.session.flush()
    # sanity check
    if billable_items_locked and old_price != registration.price:
        raise Exception("There was an error while modifying your registration (price mismatch: %s / %s)",
                        old_price, registration.price)
    notify_registration_modification(registration, notify_user)
    logger.info('Registration %s modified by %s', registration, session.user)
    regform.event_new.log(EventLogRealm.management if management else EventLogRealm.participants,
                          EventLogKind.change, 'Registration',
                          'Registration modified: {}'.format(registration.full_name),
                          session.user, data={'Email': registration.email})


def generate_spreadsheet_from_registrations(registrations, regform_items, static_items):
    """Generates a spreadsheet data from a given registration list.

    :param registrations: The list of registrations to include in the file
    :param regform_items: The registration form items to be used as columns
    :param static_items: Registration form information as extra columns
    """
    field_names = ['ID', 'Name']
    special_item_mapping = OrderedDict([
        ('reg_date', ('Registration date', lambda x: to_unicode(format_datetime(x.submitted_dt)))),
        ('state', ('Registration state', lambda x: x.state.title)),
        ('price', ('Price', lambda x: x.render_price())),
        ('checked_in', ('Checked in', lambda x: x.checked_in)),
        ('checked_in_date', ('Check-in date', lambda x: (to_unicode(format_datetime(x.checked_in_dt)) if x.checked_in
                                                         else '')))
    ])
    for item in regform_items:
        field_names.append(unique_col(item.title, item.id))
        if item.input_type == 'accommodation':
            field_names.append(unique_col('{} ({})'.format(item.title, 'Arrival'), item.id))
            field_names.append(unique_col('{} ({})'.format(item.title, 'Departure'), item.id))
    field_names.extend(title for name, (title, fn) in special_item_mapping.iteritems() if name in static_items)
    rows = []
    for registration in registrations:
        data = registration.data_by_field
        registration_dict = {
            'ID': registration.friendly_id,
            'Name': "{} {}".format(registration.first_name, registration.last_name)
        }
        for item in regform_items:
            key = unique_col(item.title, item.id)
            if item.input_type == 'accommodation':
                registration_dict[key] = data[item.id].friendly_data.get('choice') if item.id in data else ''
                key = unique_col('{} ({})'.format(item.title, 'Arrival'), item.id)
                arrival_date = data[item.id].friendly_data.get('arrival_date') if item.id in data else None
                registration_dict[key] = format_date(arrival_date) if arrival_date else ''
                key = unique_col('{} ({})'.format(item.title, 'Departure'), item.id)
                departure_date = data[item.id].friendly_data.get('departure_date') if item.id in data else None
                registration_dict[key] = format_date(departure_date) if departure_date else ''
            else:
                registration_dict[key] = data[item.id].friendly_data if item.id in data else ''
        for name, (title, fn) in special_item_mapping.iteritems():
            if name not in static_items:
                continue
            value = fn(registration)
            registration_dict[title] = value
        rows.append(registration_dict)
    return field_names, rows


def get_registrations_with_tickets(user, event):
    return Registration.find(Registration.user == user,
                             Registration.state == RegistrationState.complete,
                             RegistrationForm.event_id == event.id,
                             RegistrationForm.tickets_enabled,
                             RegistrationForm.ticket_on_event_page,
                             ~RegistrationForm.is_deleted,
                             ~Registration.is_deleted,
                             _join=Registration.registration_form).all()


def get_published_registrations(event):
    """Get a list of published registrations for an event.

    :param event: the `Event` to get registrations for
    :return: list of `Registration` objects
    """
    return (Registration
            .find(Registration.is_active,
                  ~RegistrationForm.is_deleted,
                  RegistrationForm.event_id == event.id,
                  RegistrationForm.publish_registrations_enabled,
                  _join=Registration.registration_form,
                  _eager=Registration.registration_form)
            .order_by(db.func.lower(Registration.first_name),
                      db.func.lower(Registration.last_name),
                      Registration.friendly_id)
            .all())


def get_events_registered(user, from_dt=None, to_dt=None):
    """Gets the IDs of events where the user is registered.

    :param user: A `User`
    :param from_dt: The earliest event start time to look for
    :param to_dt: The latest event start time to look for
    :return: A set of event ids
    """
    query = (user.registrations
             .options(load_only('event_id'))
             .options(joinedload(Registration.registration_form).load_only('event_id'))
             .join(Registration.registration_form)
             .join(RegistrationForm.event_new)
             .filter(Registration.is_active, ~RegistrationForm.is_deleted, ~Event.is_deleted,
                     Event.starts_between(from_dt, to_dt)))
    return {registration.event_id for registration in query}


def build_registrations_api_data(event):
    api_data = []
    query = (event.registration_forms
             .filter_by(is_deleted=False)
             .options(joinedload('registrations').joinedload('data').joinedload('field_data')))
    for regform in query:
        for registration in regform.active_registrations:
            registration_info = _build_base_registration_info(registration)
            registration_info['checkin_secret'] = registration.ticket_uuid
            api_data.append(registration_info)
    return api_data


def _build_base_registration_info(registration):
    personal_data = _build_personal_data(registration)
    return {
        'registrant_id': str(registration.id),
        'checked_in': registration.checked_in,
        'checkin_secret': registration.ticket_uuid,
        'full_name': '{} {}'.format(personal_data.get('title', ''), registration.full_name),
        'personal_data': personal_data
    }


def _build_personal_data(registration):
    personal_data = registration.get_personal_data()
    personal_data['firstName'] = personal_data.pop('first_name')
    personal_data['surname'] = personal_data.pop('last_name')
    personal_data['country'] = personal_data.pop('country', '')
    personal_data['phone'] = personal_data.pop('phone', '')
    return personal_data


def build_registration_api_data(registration):
    registration_info = _build_base_registration_info(registration)
    registration_info['amount_paid'] = registration.price if registration.is_paid else 0
    registration_info['registration_date'] = registration.submitted_dt.isoformat()
    registration_info['paid'] = registration.is_paid
    registration_info['checkin_date'] = registration.checked_in_dt.isoformat() if registration.checked_in_dt else ''
    registration_info['event_id'] = registration.event_id
    return registration_info


class RegistrationListGenerator(ListGeneratorBase):
    """Listing and filtering actions in the registration list."""

    endpoint = '.manage_reglist'
    list_link_type = 'registration'

    def __init__(self, regform):
        super(RegistrationListGenerator, self).__init__(regform.event_new, entry_parent=regform)
        self.regform = regform
        self.default_list_config = {
            'items': ('title', 'email', 'affiliation', 'reg_date', 'state'),
            'filters': {'fields': {}, 'items': {}}
        }
        self.static_items = OrderedDict([
            ('reg_date', {
                'title': _('Registation Date'),
            }),
            ('price', {
                'title': _('Price'),
            }),
            ('state', {
                'title': _('State'),
                'filter_choices': {str(state.value): state.title for state in RegistrationState}
            }),
            ('checked_in', {
                'title': _('Checked in'),
                'filter_choices': {
                    '0': _('No'),
                    '1': _('Yes')
                }
            }),
            ('checked_in_date', {
                'title': _('Check-in date'),
            })
        ])
        self.personal_items = ('title', 'first_name', 'last_name', 'email', 'position', 'affiliation', 'address',
                               'phone', 'country')
        self.list_config = self._get_config()

    def _get_static_columns(self, ids):
        """
        Retrieve information needed for the header of the static
        columns (including static and personal items).

        :return: a list of {'id': ..., 'caption': ...} dicts
        """
        result = []
        for item_id in ids:
            if item_id in self.personal_items:
                field = RegistrationFormItem.find_one(registration_form=self.regform,
                                                      personal_data_type=PersonalDataType[item_id])
                result.append({
                    'id': field.id,
                    'caption': field.title
                })
            elif item_id in self.static_items:
                result.append({
                    'id': item_id,
                    'caption': self.static_items[item_id]['title']
                })
        return result

    def _column_ids_to_db(self, ids):
        """Translate string-based ids to DB-based RegistrationFormItem ids."""
        result = []
        for item_id in ids:
            if isinstance(item_id, basestring):
                personal_data = PersonalDataType.get(item_id)
                if personal_data:
                    result.append(RegistrationFormPersonalDataField.find_one(registration_form=self.regform,
                                                                             personal_data_type=personal_data).id)
                else:
                    result.append(item_id)
            else:
                result.append(item_id)
        return result

    def _get_sorted_regform_items(self, item_ids):
        """Return the form items ordered by their position in the registration form."""

        return (RegistrationFormItem
                .find(~RegistrationFormItem.is_deleted, RegistrationFormItem.id.in_(item_ids))
                .with_parent(self.regform)
                .join(RegistrationFormItem.parent, aliased=True)
                .filter(~RegistrationFormItem.is_deleted)  # parent deleted
                .order_by(RegistrationFormItem.position)  # parent position
                .reset_joinpoint()
                .order_by(RegistrationFormItem.position)  # item position
                .all())

    def _get_filters_from_request(self):
        filters = super(RegistrationListGenerator, self)._get_filters_from_request()
        for field in self.regform.form_items:
            if field.is_field and field.input_type in {'single_choice', 'multi_choice', 'country', 'bool', 'checkbox'}:
                options = request.form.getlist('field_{}'.format(field.id))
                if options:
                    filters['fields'][str(field.id)] = options
        return filters

    def _build_query(self):
        return (Registration.query
                .with_parent(self.regform)
                .filter(~Registration.is_deleted)
                .options(joinedload('data').joinedload('field_data').joinedload('field'))
                .order_by(db.func.lower(Registration.last_name), db.func.lower(Registration.first_name)))

    def _filter_list_entries(self, query, filters):
        if not (filters.get('fields') or filters.get('items')):
            return query
        field_types = {str(f.id): f.field_impl for f in self.regform.form_items
                       if f.is_field and not f.is_deleted and (f.parent_id is None or not f.parent.is_deleted)}
        field_filters = {field_id: data_list
                         for field_id, data_list in filters['fields'].iteritems()
                         if field_id in field_types}
        if not field_filters and not filters['items']:
            return query
        criteria = [db.and_(RegistrationFormFieldData.field_id == field_id,
                            field_types[field_id].create_sql_filter(data_list))
                    for field_id, data_list in field_filters.iteritems()]
        items_criteria = []
        if 'checked_in' in filters['items']:
            checked_in_values = filters['items']['checked_in']
            # If both values 'true' and 'false' are selected, there's no point in filtering
            if len(checked_in_values) == 1:
                items_criteria.append(Registration.checked_in == bool(int(checked_in_values[0])))

        if 'state' in filters['items']:
            states = [RegistrationState(int(state)) for state in filters['items']['state']]
            items_criteria.append(Registration.state.in_(states))

        if field_filters:
                subquery = (RegistrationData.query
                            .with_entities(db.func.count(RegistrationData.registration_id))
                            .join(RegistrationData.field_data)
                            .filter(RegistrationData.registration_id == Registration.id)
                            .filter(db.or_(*criteria))
                            .correlate(Registration)
                            .as_scalar())
                query = query.filter(subquery == len(field_filters))
        return query.filter(db.or_(*items_criteria))

    def get_list_kwargs(self):
        reg_list_config = self._get_config()
        registrations_query = self._build_query()
        total_entries = registrations_query.count()
        registrations = self._filter_list_entries(registrations_query, reg_list_config['filters']).all()
        dynamic_item_ids, static_item_ids = self._split_item_ids(reg_list_config['items'], 'dynamic')
        static_columns = self._get_static_columns(static_item_ids)
        regform_items = self._get_sorted_regform_items(dynamic_item_ids)
        return {
            'registrations': registrations,
            'total_registrations': total_entries,
            'static_columns': static_columns,
            'dynamic_columns': regform_items,
            'filtering_enabled': total_entries != len(registrations)
        }

    def get_list_export_config(self):
        reg_list_config = self._get_config()
        static_item_ids, item_ids = self._split_item_ids(reg_list_config['items'], 'static')
        item_ids = self._column_ids_to_db(item_ids)
        return {
            'static_item_ids': static_item_ids,
            'regform_items': self._get_sorted_regform_items(item_ids)
        }

    def render_list(self):
        reg_list_kwargs = self.get_list_kwargs()
        tpl = get_template_module('events/registration/management/_reglist.html')
        filtering_enabled = reg_list_kwargs.pop('filtering_enabled')
        return {
            'html': tpl.render_registration_list(**reg_list_kwargs),
            'filtering_enabled': filtering_enabled
        }

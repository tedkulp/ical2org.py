#!/usr/bin/env python3

from __future__ import print_function
from math import floor
from datetime import datetime, timedelta
from hashlib import md5
import itertools
from icalendar import Calendar
from pytz import timezone, utc, all_timezones
from tzlocal import get_localzone
import click
from pprint import pprint
from dateutil.rrule import rrulestr, rruleset

def org_datetime(dt, tz):
    '''Timezone aware datetime to YYYY-MM-DD DayofWeek HH:MM str in localtime.
    '''
    return dt.astimezone(tz).strftime("<%Y-%m-%d %a %H:%M>")

def org_date(dt, tz):
    '''Timezone aware date to YYYY-MM-DD DayofWeek in localtime.
    '''
    return dt.astimezone(tz).strftime("<%Y-%m-%d %a>")

def format_datetime(dt, tz):
    '''Timezone aware datetime to YYYY-MM-DD HH:MM str in localtime.
    '''
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")

def get_datetime(dt, tz):
    '''Convert date or datetime to local datetime.
    '''
    if isinstance(dt, datetime):
        if not dt.tzinfo:
            return dt.replace(tzinfo = tz)
        return dt
    # d is date. Being a naive date, let's suppose it is in local
    # timezone.  Unfortunately using the tzinfo argument of the standard
    # datetime constructors ''does not work'' with pytz for many
    # timezones, so create first a utc datetime, and convert to local
    # timezone
    aux_dt = datetime(year=dt.year, month=dt.month, day=dt.day, tzinfo=utc)
    return aux_dt.astimezone(tz)

def add_delta_dst(dt, delta):
    '''Add a timedelta to a datetime, adjusting DST when appropriate'''
    # convert datetime to naive, add delta and convert again to its own
    # timezone
    naive_dt = dt.replace(tzinfo=None)
    return dt.tzinfo.localize(naive_dt + delta)

def advance_just_before(start_dt, timeframe_start, delta_days):
    '''Advance an start_dt datetime to the first date just before
    timeframe_start. Use delta_days for advancing the event. Precond:
    start_dt < timeframe_start'''
    delta_ord = floor(
        (timeframe_start.toordinal() - start_dt.toordinal() - 1) / delta_days)
    return (add_delta_dst(
        start_dt, timedelta(days=delta_days * int(delta_ord))), int(delta_ord))

def generate_id(start_date, end_date, uid, timezone):
    str_to_hash = u"{}{}{}".format(format_datetime(start_date, timezone), format_datetime(end_date, timezone), uid)
    return md5(str_to_hash.encode()).hexdigest()

def generate_events(comp, timeframe_start, timeframe_end, tz, emails):
    '''Get iterator with the proper delta (days, weeks, etc)'''
    # Note: timeframe_start and timeframe_end are in UTC
    if comp.name != 'VEVENT':
        return []

    if 'RRULE' in comp:
        return RecurringEvent(comp, timeframe_start, timeframe_end, tz)

    return SingleEvent(comp, timeframe_start, timeframe_end, tz, emails)

def filter_events(events, comp, tz, emails):
    '''Given a set of events (datetime objects), filter out some of them according to rules in comp.
    @return remaining events
    '''
    exclude = set()
    # filter out whole event series if one attendee is in emails and his status is declined
    attL = comp.get('ATTENDEE', None)
    if attL:
        if not isinstance(attL, list):
            attL = [attL]
        for att in attL:
            if att.params.get('PARTSTAT', '') == 'DECLINED' and att.params.get('CN', '') in emails:
                return []
    if 'EXDATE' in comp:
        exdate = comp['EXDATE']
        if isinstance(exdate, list):
            exdate = itertools.chain.from_iterable([e.dts for e in exdate])
        else:
            exdate = exdate.dts
        exclude = set(get_datetime(dt.dt, tz) for dt in exdate)
    filtered_events = list()
    for ev in events:
        if ev in exclude:
            continue
        filtered_events.append(ev)
    return filtered_events

class RecurringEvent():
    '''Iterator for recurring events.'''

    def __init__(self, comp, timeframe_start, timeframe_end, tz):
        self.ev_start = get_datetime(comp['DTSTART'].dt, tz)
        if "DTEND" not in comp:
            self.ev_end = self.ev_start
        else:
            self.ev_end = get_datetime(comp['DTEND'].dt, tz)
        self.duration = self.ev_end - self.ev_start

        try:
            self.recurrences = rrulestr(comp['RRULE'].to_ical().decode('utf-8'), dtstart=self.ev_start)
        except:
            print('Could not decode RRULE: ' + comp['RRULE'].to_ical().decode('utf-8'))
            self.recurrences = []
        self.rules = rruleset()
        self.rules.rrule(self.recurrences)

        self.exclude = set()
        if 'EXDATE' in comp:
            exdate = comp['EXDATE']
            if isinstance(exdate, list):
                exdate = itertools.chain.from_iterable([e.dts for e in exdate])
            else:
                exdate = exdate.dts
            self.exclude = set([get_datetime(dt.dt, tz) for dt in exdate])

            for skip in self.exclude:
                self.rules.exdate(skip)

        self.events = self.rules.between(timeframe_start, timeframe_end)

    def __iter__(self):
        return self

    def __next__(self):
        if self.events:
            current = self.events.pop()
            return (current,
                    current.tzinfo.normalize(current+self.duration),1)
        raise StopIteration

class SingleEvent():
    '''Iterator for non-recurring single events.'''

    def __init__(self, comp, timeframe_start, timeframe_end, tz, emails):
        ev_start = get_datetime(comp['DTSTART'].dt, tz)
        # Events with the same begin/end time same do not include
        # "DTEND".
        if "DTEND" in comp:
            ev_end = get_datetime(comp['DTEND'].dt, tz)
            self.duration = ev_end - ev_start
        else:
            if "DURATION" in comp:
                self.duration = comp['DURATION'].dt
                ev_end = ev_start + self.duration
            else:
                ev_end = ev_start
        self.duration = ev_end - ev_start
        self.events = []
        if (ev_start < timeframe_end and ev_end > timeframe_start):
            self.events = [(ev_start, ev_end, 0)
                           for ev_start in filter_events([ev_start], comp, tz, emails)]
    def __iter__(self):
        return iter(self.events)

class IcalError(Exception):
    pass

class Convertor():
    RECUR_TAG = ":RECURRING:"

    # Do not change anything below

    def __init__(self, days=90, tz=None, emails = [], include_location=True):
        """
        days: Window length in days (left & right from current time). Has
        to be positive.
        tz: timezone. If None, use local timezone.
        emails: list of user email addresses (to deal with declined events)
        """
        self.emails = set(emails)
        self.tz = timezone(tz) if tz else get_localzone()
        self.days = days
        self.include_location = include_location
        self.hashes = []

    def __call__(self, fh, fh_w):
        try:
            cal = Calendar.from_ical(fh.read())
        except ValueError as e:
            msg = "Parsing error: {}".format(e)
            raise IcalError(msg)

        now = datetime.now(utc)
        start = now - timedelta(days=self.days)
        end = now + timedelta(days=self.days)
        for comp in cal.walk():
            # print(comp)
            summary = None
            if "SUMMARY" in comp:
                summary = comp['SUMMARY'].to_ical().decode("utf-8")
                summary = summary.replace('\\,', ',')
            location = None
            if "LOCATION" in comp:
                location = comp['LOCATION'].to_ical().decode("utf-8")
                location = location.replace('\\,', ',')
            if not any((summary, location)):
                summary = u"(No title)"
            else:
                summary += " - " + location if location and self.include_location else ''
            description = None
            if 'DESCRIPTION' in comp:
                description = '\n'.join(comp['DESCRIPTION'].to_ical()
                                        .decode("utf-8").split('\\n'))
                description = description.replace('\\,', ',')
            try:
                events = generate_events(comp, start, end, self.tz, self.emails)
                for comp_start, comp_end, rec_event in events:
                    uid = comp.get('UID', '**NOID**')
                    org_uid = generate_id(comp_start, comp_end, uid, self.tz)

                    # Prune duplicates
                    if org_uid in self.hashes:
                        continue
                    self.hashes.append(org_uid)

                    fh_w.write(u"* {}".format(summary))
                    if rec_event and self.RECUR_TAG:
                        fh_w.write(u" {}\n".format(self.RECUR_TAG))
                    fh_w.write(u"\n")
                    fh_w.write(u":ICALCONTENTS:\n")
                    fh_w.write(u":ORGUID: {}\n".format(org_uid))
                    fh_w.write(u":ORIGINAL-UID: {}\n".format(uid))
                    fh_w.write(u":DTSTART: {}\n".format(format_datetime(comp_start, self.tz)))
                    fh_w.write(u":DTEND: {}\n".format(format_datetime(comp_end, self.tz)))
                    fh_w.write(u":DTSTAMP: {}\n".format(format_datetime(comp['DTSTAMP'].dt, self.tz)))
                    if 'ATTENDEE' in comp:
                        for attendee in comp['ATTENDEE']:
                            fh_w.write(u":ATTENDEE: {}\n".format(attendee))
                    if 'ORGANIZER' in comp:
                        fh_w.write(u":ORGANIZER: {}\n".format(comp['ORGANIZER']))
                    if 'RRULE' in comp:
                        fh_w.write(u":RRULE: {}\n".format(comp['RRULE']))
                    fh_w.write(u":END:\n")
                    if isinstance(comp["DTSTART"].dt, datetime):
                        fh_w.write(u"  {}--{}\n".format(
                            org_datetime(comp_start, self.tz),
                            org_datetime(comp_end, self.tz)))
                    else:  # all day event
                        fh_w.write(u"  {}--{}\n".format(
                            org_date(comp_start, timezone('UTC')),
                            org_date(comp_end - timedelta(days=1), timezone('UTC'))))
                    if description:
                        fh_w.write(u"** Description\n\n")
                        fh_w.write(u"{}\n".format(description))
                    fh_w.write(u"\n")
            except Exception as e:
                msg = "Error: {}" .format(e)
                raise IcalError(msg)

def check_timezone(ctx, param, value):
    if (value is None) or (value in all_timezones):
        return value
    click.echo(u"Invalid timezone value {value}.".format(value=value))
    click.echo(u"Use --print-timezones to show acceptable values.")
    ctx.exit(1)

def print_timezones(ctx, param, value):
    if not value or ctx.resilient_parsing:
        return
    for tz in all_timezones:
        click.echo(tz)
    ctx.exit()


@click.command(context_settings={"help_option_names": ['-h', '--help']})
@click.option(
    "--print-timezones",
    "-p",
    is_flag=True,
    callback=print_timezones,
    is_eager=True,
    expose_value=False,
    help="Print acceptable timezone names and exit.")
@click.option(
    "--email",
    "-e",
    multiple=True,
    default=None,
    help="User email address (used to deal with declined events). You can write multiple emails with as many -e options as you like.")
@click.option(
    "--days",
    "-d",
    default=90,
    type=click.IntRange(0, clamp=True),
    help=("Window length in days (left & right from current time. Default is 90 days). "
          "Has to be positive."))
@click.option(
    "--timezone",
    "-t",
    default=None,
    callback=check_timezone,
    help="Timezone to use. (Local timezone by default).")
@click.option(
    "--location/--no-location",
    "include_location",
    default=True,
    help="Include the location (if present) in the headline. (Location is included by default).")
@click.argument("ics_file", type=click.File("r", encoding="utf-8"))
@click.argument("org_file", type=click.File("w", encoding="utf-8"))
def main(ics_file, org_file, email, days, timezone, include_location):
    """Convert ICAL format into org-mode.

    Files can be set as explicit file name, or `-` for stdin or stdout::

        $ ical2orgpy in.ical out.org

        $ ical2orgpy in.ical - > out.org

        $ cat in.ical | ical2orgpy - out.org

        $ cat in.ical | ical2orgpy - - > out.org
    """
    convertor = Convertor(days, timezone, email, include_location)
    try:
        convertor(ics_file, org_file)
    except IcalError as e:
        click.echo(str(e), err=True)
        raise click.Abort()

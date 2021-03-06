import logging
import os
from string import ascii_letters, digits
from typing import Optional, Set, Union
from urllib.parse import urlparse
from urllib3.exceptions import HTTPError

from bs4 import BeautifulSoup

import requests
from requests.exceptions import RequestException

from analysis.wappalyzer_apps import wappalyzer_apps
from backends.software_version import SoftwareVersion
from base.checksum import calculate_checksum
from base.utils import clean_path_name
from settings import BACKEND, HTML_PARSER, HTTP_TIMEOUT


class RetrievalFailure(Exception):
    """The retrieval of the resource has failed."""


class Resource:
    """
    A resource is any file which can be retrieved from a url.
    """
    # url: str

    def __init__(self, url: str, cache: Optional[dict] = None):
        self.url = url
        self.cache = cache

    def __eq__(self, other) -> bool:
        return self.url == other.url

    def __hash__(self) -> int:
        return hash(self.url)

    def __repr__(self) -> str:
        return "<{} '{}'>".format(str(self.__class__.__name__), str(self))

    def __str__(self) -> str:
        return self.url

    @property
    def content(self) -> bytes:
        if not self.retrieved:
            self.retrieve()
        if not self._success:
            raise RetrievalFailure

        return self._response.content

    def extract_information(self) -> Set[SoftwareVersion]:
        """
        Extract information from resource text source.
        """
        result = set()

        parsed = self._parse()

        # generator tag
        result |= self._extract_generator_tag(parsed)
        result |= self._extract_wappalyzer_information()

        return result

    def persist(self, base_path: str):
        """
        Persist this resource underneath base_path.
        """
        VALID_NAME_CHARS = ascii_letters + digits + '-_.()=[]{}\\'

        if not self.retrieved or not self.success:
            logging.info('not storing not (successfully) retrieved resource %s' % self.url)
            return

        parsed_url = urlparse(self.url)
        path_name = clean_path_name(parsed_url.path[1:]) or '__index__'

        if len(path_name) > 200:
            path_name = '...'.join((
                path_name[:50],
                calculate_checksum(path_name.encode()).hex(),
                path_name[-50:]))

        path = os.path.join(
            base_path,
            '_'.join((parsed_url.scheme, parsed_url.netloc)),
            path_name)

        logging.info('persisting resource %s at %s' % (self.url, path))

        os.makedirs(
            os.path.dirname(path),
            exist_ok=True)

        with open(path, 'wb') as fdes:
            fdes.write(self.content)

    @property
    def final_url(self) -> str:
        """The final url, i.e., the url of the resource after all redirects."""
        if not self.retrieved:
            self.retrieve()
        if not self._success:
            raise RetrievalFailure

        return self._response.url

    def retrieve(self):
        """Retrieve the resource from its url."""
        if self.cache is not None and self.url in self.cache:
            logging.info('Using cached version of resource %s', self.url)
            self._success = True
            self._response = self.cache[self.url]
            return

        logging.info('Retrieving resource %s', self.url)

        try:
            self._response = requests.get(self.url, timeout=HTTP_TIMEOUT)
        except (HTTPError, RequestException, UnicodeError):
            self._success = False
        else:
            self.cache[self.url] = self._response
            self._success = True

        if not self._success or self._response.status_code != 200:
            logging.info(
                'Retrieval failure for %s',
                self.url)

    @property
    def retrieved(self) -> bool:
        """Whether the resource has already been retrieved."""
        return hasattr(self, '_response') or hasattr(self, '_success')

    def serialize(self) -> dict:
        """Serialize into a dict."""
        if not self.success:
            return {
                'url': self.url,
                'webroot_path': self.webroot_path,
                'success': self.success,
            }
        return {
            'url': self.url,
            'webroot_path': self.webroot_path,
            'status_code': self.status_code,
            'success': self.success,
        }

    @property
    def status_code(self) -> int:
        if not self.retrieved:
            self.retrieve()
        if not self._success:
            raise RetrievalFailure

        return self._response.status_code

    @property
    def success(self) -> bool:
        if not self.retrieved:
            self.retrieve()
        return self._success and self.status_code == 200

    @property
    def webroot_path(self):
        """Get the webroot path of this asset."""
        # TODO: Add support for subdirs And similar
        url = urlparse(self.url)
        return url.path

    @staticmethod
    def _extract_generator_tag(parsed: BeautifulSoup) -> Set[SoftwareVersion]:
        """Extract information from generator tag."""
        generator_tags = parsed.find_all('meta', {'name': 'generator'})
        if len(generator_tags) != 1:
            # If none or multiple generator tags are found, that is not a
            # reliable source
            return set()

        result = set()

        generator_tag = generator_tags[0].get('content')
        if not generator_tag:
            return set()

        components = generator_tag.split()
        if not components:
            return result
        # TODO: Maybe there is already a version in the generator tag ...
        # TODO: Therefore, do not throw non-first components away
        # TODO: Software packages with spaces in name
        matches = BACKEND.retrieve_packages_by_name(components[0])
        for match in matches:
            versions = BACKEND.retrieve_versions(match)
            matching_versions = versions.copy()
            if len(components) > 1:
                # generator tag might contain version information already.
                for version in versions:
                    if components[1].lower().strip() not in version.name.lower().strip():
                        matching_versions.remove(version)
                if not matching_versions:
                    # not a single version matched
                    matching_versions = versions
            result.update(matching_versions)

        logging.info('Generator tag suggests one of: %s', result)

        return result

    def _extract_wappalyzer_information(self) -> Set[SoftwareVersion]:
        """Use wappalyzer wrapper to get version information."""
        # TODO: maybe expansion to version is a bad idea, because packages with a lot of versions get a higher weight than those with only a few releases
        app_matches = set()
        version_matches = set()
        for app in wappalyzer_apps:
            if app.matches(self._response):
                app_matches.add(app)
                version_matches |= BACKEND.retrieve_versions(app.software_package)
        logging.info('wappalyzer suggests on of %s', app_matches)
        return version_matches

    def _parse(self) -> BeautifulSoup:
        return BeautifulSoup(
            self.content,
            HTML_PARSER)

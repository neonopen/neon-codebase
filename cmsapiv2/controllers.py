#!/usr/bin/env python

import os.path
import sys
__base_path__ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if sys.path[0] != __base_path__:
    sys.path.insert(0, __base_path__)

import api.brightcove_api
from apiv2 import *
from cvutils.imageutils import PILImageUtils
import dateutil.parser
import model.predictor
import PIL.Image
import PIL.ImageDraw
import PIL.ImageFont
import re
import io
from cStringIO import StringIO
import utils.pycvutils

import cmsapiv2.client
from cmsdb.cdnhosting import AWSHosting
from cmsdb.neondata import TagType
import fractions
import logging
import model
import utils.autoscale
import video_processor.video_processing_queue
_log = logging.getLogger(__name__)

define("port", default=8084, help="run on the given port", type=int)
define("cmsapiv1_port", default=8083, help="what port apiv1 is running on",
       type=int)

# For scoring non-video thumbnails.
define('model_server_port', default=9000, type=int,
       help='the port currently being used by model servers')
define('model_autoscale_groups', default='AquilaOnDemand', type=str,
       help='Comma separated list of autoscaling group names')
define('request_concurrency', default=22, type=int,
       help=('the maximum number of concurrent scoring requests to'
             ' make at a time. Should be less than or equal to the'
             ' server batch size.'))

statemon.define('put_account_oks', int)
statemon.define('get_account_oks', int)

statemon.define('post_ooyala_oks', int)
statemon.define('put_ooyala_oks', int)
statemon.define('get_ooyala_oks', int)

statemon.define('post_brightcove_oks', int)
statemon.define('put_brightcove_oks', int)
statemon.define('get_brightcove_oks', int)

statemon.define('put_brightcove_player_oks', int)
statemon.define('get_brightcove_player_oks', int)
statemon.define('brightcove_publish_plugin_error', int)

statemon.define('post_thumbnail_oks', int)
statemon.define('put_thumbnail_oks', int)
statemon.define('get_thumbnail_oks', int)

statemon.define('post_video_oks', int)
statemon.define('put_video_oks', int)
statemon.define('get_video_oks', int)
_get_video_oks_ref = statemon.state.get_ref('get_video_oks')

statemon.define('social_image_generated', int)
statemon.define('social_image_invalid_request', int)

statemon.define('get_internal_search_oks', int)
_get_internal_search_oks_ref = statemon.state.get_ref(
    'get_internal_search_oks')

statemon.define('get_external_search_oks', int)
_get_external_search_oks_ref = statemon.state.get_ref(
    'get_external_search_oks')

'''*****************************************************************
AccountHandler
*****************************************************************'''
class AccountHandler(APIV2Handler):
    """Handles get,put requests to the account endpoint.
       Gets and updates existing accounts.
    """
    @tornado.gen.coroutine
    def get(self, account_id):
        """handles account endpoint get request"""

        schema = Schema({
          Required('account_id'): All(Coerce(str), Length(min=1, max=256)),
          'fields': Any(CustomVoluptuousTypes.CommaSeparatedList())
        })

        args = {}
        args['account_id'] = account_id = str(account_id)
        schema(args)

        fields = args.get('fields', None)
        if fields:
            fields = set(fields.split(','))

        user_account = yield tornado.gen.Task(neondata.NeonUserAccount.get,
                                              account_id)

        if not user_account:
            raise NotFoundError()

        user_account = yield self.db2api(user_account, fields=fields)
        statemon.state.increment('get_account_oks')
        self.success(user_account)

    @tornado.gen.coroutine
    def put(self, account_id):
        """handles account endpoint put request"""

        schema = Schema({
          Required('account_id'): All(Coerce(str), Length(min=1, max=256)),
          'default_width': All(Coerce(int), Range(min=1, max=8192)),
          'default_height': All(Coerce(int), Range(min=1, max=8192)),
          'default_thumbnail_id': All(Coerce(str), Length(min=1, max=2048))
        })
        args = self.parse_args()
        args['account_id'] = str(account_id)
        schema(args)
        acct_internal = yield tornado.gen.Task(neondata.NeonUserAccount.get,
                                               args['account_id'])
        if not acct_internal:
            raise NotFoundError()

        acct_for_return = yield self.db2api(acct_internal)
        def _update_account(a):
            a.default_size = list(a.default_size)
            a.default_size[0] = int(args.get('default_width',
                                             acct_internal.default_size[0]))
            a.default_size[1] = int(args.get('default_height',
                                             acct_internal.default_size[1]))
            a.default_size = tuple(a.default_size)
            a.default_thumbnail_id = args.get(
                'default_thumbnail_id',
                acct_internal.default_thumbnail_id)

        yield tornado.gen.Task(neondata.NeonUserAccount.modify,
                               acct_internal.key, _update_account)
        statemon.state.increment('put_account_oks')
        self.success(acct_for_return)

    @classmethod
    def get_access_levels(cls):
        return {
                 HTTPVerbs.GET: neondata.AccessLevels.READ,
                 HTTPVerbs.PUT: neondata.AccessLevels.UPDATE,
                 'account_required': [HTTPVerbs.GET, HTTPVerbs.PUT]
               }

    @classmethod
    def _get_default_returned_fields(cls):
        return ['account_id', 'default_size', 'customer_name',
                'default_thumbnail_id', 'tracker_account_id',
                'staging_tracker_account_id',
                'integration_ids', 'created', 'updated', 'users',
                'serving_enabled', 'email']

    @classmethod
    def _get_passthrough_fields(cls):
        return ['default_size',
                'default_thumbnail_id', 'tracker_account_id',
                'staging_tracker_account_id',
                'created', 'updated', 'users',
                'serving_enabled', 'email']

    @classmethod
    @tornado.gen.coroutine
    def _convert_special_field(cls, obj, field):
        if field == 'account_id':
            # this is weird, but neon_api_key is actually the
            # "id" on this table, it's what we use to get information
            # about the account, so send back api_key (as account_id)
            retval = obj.neon_api_key
        elif field == 'customer_name':
            retval = obj.name
        elif field == 'integration_ids':
            retval = obj.integrations.keys()
        else:
            raise BadRequestError('invalid field %s' % field)

        raise tornado.gen.Return(retval)


'''*********************************************************************
IntegrationHelper
*********************************************************************'''
class IntegrationHelper(object):
    """Class responsible for helping the integration handlers."""

    @staticmethod
    @tornado.gen.coroutine
    def create_integration(acct, args, integration_type, cdn=None):
        """Creates an integration for any integration type.

        Keyword arguments:
        acct - a NeonUserAccount object
        args - the args sent in via the API request
        integration_type - the type of integration to create
        schema - validate args with this Voluptuous schema
        cdn - an optional CDNHostingMetadata object to intialize the
              CDNHosting with
        """

        integration = None
        if integration_type == neondata.IntegrationType.OOYALA:
            integration = neondata.OoyalaIntegration()
            integration.account_id = acct.neon_api_key
            integration.partner_code = args['publisher_id']
            integration.api_key = args.get('api_key', integration.api_key)
            integration.api_secret = args.get('api_secret',
                                              integration.api_secret)

        elif integration_type == neondata.IntegrationType.BRIGHTCOVE:
            integration = neondata.BrightcoveIntegration()
            integration.account_id = acct.neon_api_key
            integration.publisher_id = args['publisher_id']

            integration.read_token = args.get(
                'read_token',
                integration.read_token)
            integration.write_token = args.get(
                'write_token',
                integration.write_token)
            integration.application_client_id = args.get(
                'application_client_id',
                integration.application_client_id)
            integration.application_client_secret = args.get(
                'application_client_secret',
                integration.application_client_secret)
            integration.callback_url = args.get(
                'callback_url',
                integration.callback_url)
            playlist_feed_ids = args.get('playlist_feed_ids', None)

            if playlist_feed_ids:
                integration.playlist_feed_ids = playlist_feed_ids.split(',')

            integration.id_field = args.get(
                'id_field',
                integration.id_field)
            integration.uses_batch_provisioning = Boolean()(args.get(
                'uses_batch_provisioning',
                integration.uses_batch_provisioning))
            integration.uses_bc_gallery = Boolean()(args.get(
                'uses_bc_gallery',
                integration.uses_bc_gallery))
            integration.uses_bc_thumbnail_api = Boolean()(args.get(
                'uses_bc_thumbnail_api',
                integration.uses_bc_thumbnail_api))
            integration.uses_bc_videojs_player = Boolean()(args.get(
                'uses_bc_videojs_player',
                integration.uses_bc_videojs_player))
            integration.uses_bc_smart_player = Boolean()(args.get(
                'uses_bc_smart_player',
                integration.uses_bc_smart_player))
            integration.last_process_date = args.get(
                'last_process_date',
                integration.last_process_date)
        else:
            raise ValueError('Unknown integration type')

        if cdn:
            cdn_list = neondata.CDNHostingMetadataList(
                neondata.CDNHostingMetadataList.create_key(
                    acct.neon_api_key,
                    integration.get_id()),
                [cdn])
            success = yield cdn_list.save(async=True)
            if not success:
                raise SaveError('unable to save CDN hosting')

        success = yield integration.save(async=True)
        if not success:
            raise SaveError('unable to save Integration')

        raise tornado.gen.Return(integration)

    @staticmethod
    @tornado.gen.coroutine
    def get_integration(integration_id, integration_type):
        """Gets an integration based on integration_id, account_id, and type.

        Keyword arguments:
        account_id - the account_id that owns the integration
        integration_id - the integration_id of the integration we want
        integration_type - the type of integration to create
        """
        if integration_type == neondata.IntegrationType.OOYALA:
            integration = yield tornado.gen.Task(
                neondata.OoyalaIntegration.get,
                integration_id)
        elif integration_type == neondata.IntegrationType.BRIGHTCOVE:
            integration = yield tornado.gen.Task(
                neondata.BrightcoveIntegration.get,
                integration_id)
        if integration:
            raise tornado.gen.Return(integration)
        else:
            raise NotFoundError('%s %s' % ('unable to find the integration '
                                           'for id:',integration_id))

    @staticmethod
    @tornado.gen.coroutine
    def get_integrations(account_id):
        """ gets all integrations for an account.

        Keyword arguments
        account_id - the account_id that is associated with the integrations
        """
        user_account = yield neondata.NeonUserAccount.get(
            account_id,
            async=True)

        if not user_account:
            raise NotFoundError()

        integrations = yield user_account.get_integrations(async=True)
        rv = {}
        rv['integrations'] = []
        for i in integrations:
            new_obj = None
            if type(i).__name__.lower() == neondata.IntegrationType.BRIGHTCOVE:
                new_obj = yield BrightcoveIntegrationHandler.db2api(i)
                new_obj['type'] = 'brightcove'
            elif type(i).__name__.lower() == neondata.IntegrationType.OOYALA:
                new_obj = yield OoyalaIntegrationHandler.db2api(i)
                new_obj['type'] = 'ooyala'
            else:
                continue

            if new_obj:
                rv['integrations'].append(new_obj)

        raise tornado.gen.Return(rv)

    @staticmethod
    @tornado.gen.coroutine
    def validate_oauth_credentials(client_id, client_secret, integration_type):
        if integration_type is neondata.IntegrationType.BRIGHTCOVE:
            if client_id and not client_secret:
                raise BadRequestError(
                    'App id cannot be valued if secret is not also valued')
            if client_secret and not client_id:
                raise BadRequestError(
                    'App secret cannot be valued if id is not also valued')
            # TODO validate with BC that keys are valid and the granted
            # permissions are as expected. (This is implemented in the
            # Oauth feature branch. Need to invoke it here after merge)
        elif integration_type is neondata.IntegrationType.OOYALA:
            # Implement for Ooyala
            pass

'''*********************************************************************
OoyalaIntegrationHandler
*********************************************************************'''
class OoyalaIntegrationHandler(APIV2Handler):
    """Handles get,put,post requests to the ooyala endpoint within the v2 api."""
    @tornado.gen.coroutine
    def post(self, account_id):
        """Handles an ooyala endpoint post request

        Keyword arguments:
        """
        schema = Schema({
            Required('account_id') : All(Coerce(str), Length(min=1, max=256)),
            Required('publisher_id') : All(Coerce(str), Length(min=1, max=256)),
            'api_key': All(Coerce(str), Length(min=1, max=1024)),
            'api_secret': All(Coerce(str), Length(min=1, max=1024))
        })
        args = self.parse_args()
        args['account_id'] = str(account_id)
        schema(args)

        acct = yield neondata.NeonUserAccount.get(
            args['account_id'],
            async=True)
        integration = yield tornado.gen.Task(
            IntegrationHelper.create_integration, acct, args,
            neondata.IntegrationType.OOYALA)
        statemon.state.increment('post_ooyala_oks')
        rv = yield self.db2api(integration)
        self.success(rv)

    @tornado.gen.coroutine
    def get(self, account_id):
        """handles an ooyala endpoint get request"""

        schema = Schema({
          Required('account_id'): All(Coerce(str), Length(min=1, max=256)),
          Required('integration_id'): All(Coerce(str), Length(min=1, max=256)),
          'fields': Any(CustomVoluptuousTypes.CommaSeparatedList())
        })
        args = self.parse_args()
        args['account_id'] = account_id = str(account_id)
        schema(args)

        fields = args.get('fields', None)
        if fields:
            fields = set(fields.split(','))

        integration_id = args['integration_id']
        integration = yield IntegrationHelper.get_integration(
            integration_id,
            neondata.IntegrationType.OOYALA)

        statemon.state.increment('get_ooyala_oks')
        rv = yield self.db2api(integration, fields=fields)
        self.success(rv)

    @tornado.gen.coroutine
    def put(self, account_id):
        """handles an ooyala endpoint put request"""

        schema = Schema({
          Required('account_id'): All(Coerce(str), Length(min=1, max=256)),
          Required('integration_id'): All(Coerce(str), Length(min=1, max=256)),
          'api_key': All(Coerce(str), Length(min=1, max=1024)),
          'api_secret': All(Coerce(str), Length(min=1, max=1024)),
          'publisher_id': All(Coerce(str), Length(min=1, max=1024))
        })
        args = self.parse_args()
        args['account_id'] = account_id = str(account_id)
        schema(args)
        integration_id = args['integration_id']

        integration = yield IntegrationHelper.get_integration(
            integration_id, neondata.IntegrationType.OOYALA)

        def _update_integration(p):
            p.api_key = args.get('api_key', integration.api_key)
            p.api_secret = args.get('api_secret', integration.api_secret)
            p.partner_code = args.get('publisher_id', integration.partner_code)

        yield neondata.OoyalaIntegration.modify(
            integration_id, _update_integration, async=True)

        yield IntegrationHelper.get_integration(
            integration_id, neondata.IntegrationType.OOYALA)

        statemon.state.increment('put_ooyala_oks')
        rv = yield self.db2api(integration)
        self.success(rv)

    @classmethod
    def _get_default_returned_fields(cls):
        return [ 'integration_id', 'account_id', 'partner_code',
                 'api_key', 'api_secret' ]

    @classmethod
    def _get_passthrough_fields(cls):
        return [ 'integration_id', 'account_id', 'partner_code',
                 'api_key', 'api_secret' ]

    @classmethod
    def get_access_levels(self):
        return {
                 HTTPVerbs.GET: neondata.AccessLevels.READ,
                 HTTPVerbs.POST: neondata.AccessLevels.CREATE,
                 HTTPVerbs.PUT: neondata.AccessLevels.UPDATE,
                 'account_required': [HTTPVerbs.GET,
                                        HTTPVerbs.PUT,
                                        HTTPVerbs.POST]
               }

'''*********************************************************************
BrightcovePlayerHandler
*********************************************************************'''
class BrightcovePlayerHandler(APIV2Handler):
    """Handle requests to Brightcove player endpoint"""

    @tornado.gen.coroutine
    def get(self, account_id):
        """Get the list of BrightcovePlayers for the given integration"""

        # Validate request and data
        schema = Schema({
            Required('account_id'): All(Coerce(str), Length(min=1, max=256)),
            Required('integration_id'): All(Coerce(str), Length(min=1, max=256))
        })
        args = self.parse_args()
        args['account_id'] = account_id = str(account_id)
        schema(args)
        integration_id = args['integration_id']
        integration = yield neondata.BrightcoveIntegration.get(
            integration_id,
            async=True)
        if not integration:
            raise NotFoundError(
                'BrighcoveIntegration does not exist for player reference:%s',
                args['player_ref'])

        # Retrieve the list of players from Brightcove api
        bc = api.brightcove_api.PlayerAPI(integration)
        r = yield bc.get_players()
        players = [p for p in r.get('items', []) if p['id'] != 'default']

        # @TODO batch transform dict-players to object-players
        objects  = yield map(self._bc_to_obj, players)
        ret_list = yield map(self.db2api, objects)


        # Envelope with players:, player_count:
        response = {
            'players': ret_list,
            'player_count': len(ret_list)
        }
        statemon.state.increment('get_brightcove_player_oks')
        self.success(response)

    @staticmethod
    @tornado.gen.coroutine
    def _bc_to_obj(bc_player):
        '''Retrieve or create a BrightcovePlayer from db given BC data

        If creating object, the object is not saved to the database.
        '''
        # Get the database record. Expect many to be missing, so don't log
        neon_player = yield neondata.BrightcovePlayer.get(
            bc_player['id'],
            async=True,
            log_missing=False)
        if neon_player:
            # Prefer Brightcove's data since it is potentially newer
            neon_player.name = bc_player['name']
        else:
            neon_player = neondata.BrightcovePlayer(
                player_ref=bc_player['id'],
                name=bc_player['name'])
        raise tornado.gen.Return(neon_player)

    @tornado.gen.coroutine
    def put(self, account_id):
        """Update a BrightcovePlayer tracking status and return the player

        Setting the is_tracked flag to True, will also publish the player
        via Brightcove's player management api.
        """

        # The only field that is set via public api is is_tracked.
        schema = Schema({
            Required('account_id'): All(Coerce(str), Length(min=1, max=256)),
            Required('integration_id'): All(Coerce(str), Length(min=1, max=256)),
            Required('player_ref'): All(Coerce(str), Length(min=1, max=256)),
            Required('is_tracked'): Boolean()
        })
        args = self.parse_args()
        args['account_id'] = account_id = str(account_id)
        schema(args)
        ref = args['player_ref']

        integration = yield neondata.BrightcoveIntegration.get(
            args['integration_id'],
            async=True)
        if not integration:
            raise NotFoundError(
                'BrighcoveIntegration does not exist for integration_id:%s',
                args['integration_id'])
        if integration.account_id != account_id:
            raise NotAuthorizedError('Player is not owned by this account')

        # Verify player_ref is at Brightcove
        bc = api.brightcove_api.PlayerAPI(integration)
        # This will error (expect 404) if player not found
        try:
            bc_player = yield bc.get_player(ref)
        except Exception as e:
            statemon.state.increment('brightcove_publish_plugin_error')
            raise e

        # Get or create db record
        def _modify(p):
            p.is_tracked = Boolean()(args['is_tracked'])
            p.name = bc_player['name'] # BC's name is newer
            p.integration_id = integration.integration_id
        player = yield neondata.BrightcovePlayer.modify(
            ref,
            _modify,
            create_missing=True,
            async=True)
        bc_player_config = bc_player['branches']['master']['configuration']

        # If the player is tracked, then send a request to Brightcove's
        # player managament API to put the plugin in the player
        # and publish the player.  We do this any time the user calls
        # this API with is_tracked=True because they are likely to be
        # troubleshooting their setup and publishing several times.

        # Alternatively, if player is not tracked, then send a request
        # to remove the player from the config and publish the player.
        if player.is_tracked:
            patch = BrightcovePlayerHelper._install_plugin_patch(
                bc_player_config,
                self.account.tracker_account_id)
            try:
                yield BrightcovePlayerHelper.publish_player(ref, patch, bc)
            except Exception as e:
                statemon.state.increment('brightcove_publish_plugin_error')
                raise e
            # Published. Update the player with the date and version
            def _modify(p):
                p.publish_date = datetime.now().isoformat()
                p.published_plugin_version = \
                    BrightcovePlayerHelper._get_current_tracking_version()
                p.last_attempt_result = None
            yield neondata.BrightcovePlayer.modify(ref, _modify, async=True)

        elif player.is_tracked is False:
            patch = BrightcovePlayerHelper._uninstall_plugin_patch(
                bc_player_config)
            if patch:
                try:
                    yield BrightcovePlayerHelper.publish_player(ref, patch, bc)
                except Exception as e:
                    statemon.state.increment('brightcove_publish_plugin_error')
                    raise e

        # Finally, respond with the current version of the player
        player = yield neondata.BrightcovePlayer.get(
            player.get_id(),
            async=True)
        response = yield self.db2api(player)
        statemon.state.increment('put_brightcove_player_oks')
        self.success(response)

    @classmethod
    def get_access_levels(self):
        return {
            HTTPVerbs.GET: neondata.AccessLevels.READ,
            HTTPVerbs.POST: neondata.AccessLevels.CREATE,
            HTTPVerbs.PUT: neondata.AccessLevels.UPDATE,
            'account_required': [HTTPVerbs.GET, HTTPVerbs.PUT, HTTPVerbs.POST]}

    @classmethod
    def _get_default_returned_fields(cls):
        return ['player_ref', 'name', 'is_tracked',
                'created', 'updated', 'publish_date',
                'published_plugin_version', 'last_attempt_result']

    @classmethod
    def _get_passthrough_fields(cls):
        # Player ref is transformed with get_id
        return ['name', 'is_tracked',
                'created', 'updated',
                'publish_date', 'published_plugin_version',
                'last_attempt_result']

    @classmethod
    @tornado.gen.coroutine
    def _convert_special_field(cls, obj, field):
        if field == 'player_ref':
            # Translate key to player_ref
            raise tornado.gen.Return(obj.get_id())
        raise BadRequestError('invalid field %s' % field)


'''*********************************************************************
BrightcovePlayerHelper
*********************************************************************'''

class BrightcovePlayerHelper():
    '''Contain functions that work on Players that are called internally.'''
    @staticmethod
    @tornado.gen.coroutine
    def publish_player(player_ref, patch, bc_api):
        """Update Brightcove player with patch and publishes it

        Assumes that the BC player referenced by player_ref is valid.

        Input-
        player_ref - Brightcove player reference
        patch - Dictionary of player configuration defined by Brightcove
        bc_api - Instance of Brightcove API with appropriate integration
        """
        yield bc_api.patch_player(player_ref, patch)
        yield bc_api.publish_player(player_ref)

    @staticmethod
    def _install_plugin_patch(player_config, tracker_account_id):
        """Make a patch that replaces our js and json with the current version

        Brightcove player's configuration api allows PUT to replace the entire
        configuration branch (master or preview). It allows and recommends PATCH
        to set any subset of fields. For our goal, the "plugins" field is a list
        that will be changed to a json payload that includes the Neon account id
        for tracking. The "scripts" field is a list of urls that includes our
        our minified javascript plugin url.

        Grabs the current values of the lists to change, removes any Neon info,
        then addends the Neon js url and json values with current ones.

        Inputs-
        player_config dict containing a configuration branch from Brightcove
        tracker_account_id neon tracking id for the publisher
        """

        # Remove Neon plugins from the config
        patch = BrightcovePlayerHelper._uninstall_plugin_patch(player_config)
        patch = patch if patch else {'scripts': [], 'plugins': []}

        # Append the current plugin
        patch['plugins'].append(BrightcovePlayerHelper._get_current_tracking_json(
            tracker_account_id))
        patch['scripts'].append(BrightcovePlayerHelper._get_current_tracking_url())

        return patch

    @staticmethod
    def _uninstall_plugin_patch(player_config):
        """Make a patch that removes any Neon plugin js or json"""
        plugins = [plugin for plugin in player_config.get('plugins')
            if plugin['name'] != 'neon']
        scripts = [script for script in player_config.get('scripts')
            if script.find('videojs-neon-') == -1]

        # If nothing changed, signal to caller no need to patch.
        if(len(plugins) == len(player_config['plugins']) and
                len(scripts) == len(player_config['scripts'])):
            return None

        return {
            'plugins': plugins,
            'scripts': scripts
        }

    @staticmethod
    def _get_current_tracking_version():
        """Get the version of the current tracking plugin"""
        return '0.0.1'

    @staticmethod
    def _get_current_tracking_url():
        """Get the url of the current tracking plugin"""
        return 'https://s3.amazonaws.com/neon-cdn-assets/videojs-neon-plugin.min.js'

    @staticmethod
    def _get_current_tracking_json(tracker_account_id):
        """Get JSON string that configures the plugin given the account_id

        These are options that injected into the plugin environment and override
        its defaults. { name, options { publisher { id }}} are required. Other
        flags can be found in the neon-videojs-plugin js."""

        return {
            'name': 'neon',
            'options': {
                'publisher': {
                    'id': tracker_account_id
                }
            }
        }

'''*********************************************************************
BrightcoveIntegrationHandler
*********************************************************************'''
class BrightcoveIntegrationHandler(APIV2Handler):
    """handles all requests to the brightcove endpoint within the v2 API"""
    @tornado.gen.coroutine
    def post(self, account_id):
        """handles a brightcove endpoint post request"""

        schema = Schema({
            Required('account_id'): All(Coerce(str), Length(min=1, max=256)),
            Required('publisher_id'): All(Coerce(str), Length(min=1, max=256)),
            'read_token': All(Coerce(str), Length(min=1, max=512)),
            'write_token': All(Coerce(str), Length(min=1, max=512)),
            'application_client_id': All(Coerce(str), Length(min=1, max=1024)),
            'application_client_secret': All(Coerce(str), Length(min=1, max=1024)),
            'callback_url': All(Coerce(str), Length(min=1, max=1024)),
            'id_field': All(Coerce(str), Length(min=1, max=32)),
            'playlist_feed_ids': All(CustomVoluptuousTypes.CommaSeparatedList()),
            'uses_batch_provisioning': Boolean(),
            'uses_bc_thumbnail_api': Boolean(),
            'uses_bc_videojs_player': Boolean(),
            'uses_bc_smart_player': Boolean(),
            Required('uses_bc_gallery'): Boolean()
        })
        args = self.parse_args()
        args['account_id'] = str(account_id)
        schema(args)
        publisher_id = args.get('publisher_id')

        # Check credentials with Brightcove's CMS API.
        client_id = args.get('application_client_id')
        client_secret = args.get('application_client_secret')
        IntegrationHelper.validate_oauth_credentials(
            client_id=client_id,
            client_secret=client_secret,
            integration_type=neondata.IntegrationType.BRIGHTCOVE)

        acct = yield neondata.NeonUserAccount.get(
            args['account_id'],
            async=True)

        if not acct:
            raise NotFoundError('Neon Account required.')

        app_id = args.get('application_client_id', None)
        app_secret = args.get('application_client_secret', None)

        if app_id or app_secret:
            # Check credentials with Brightcove's CMS API.
            IntegrationHelper.validate_oauth_credentials(
                client_id=app_id,
                client_secret=app_secret,
                integration_type=neondata.IntegrationType.BRIGHTCOVE)
            # Excecute a search and get last_processed_date

            lpd = yield self._get_last_processed_date(
                publisher_id,
                app_id,
                app_secret)

            if lpd:
                args['last_process_date'] = lpd
            else:
                raise BadRequestError('Brightcove credentials are bad, ' \
                    'application_id or application_secret are wrong.')

        cdn = None
        if Boolean()(args['uses_bc_gallery']):
            # We have a different set of image sizes to generate for
            # Gallery, so setup the CDN
            cdn = neondata.NeonCDNHostingMetadata(
                rendition_sizes = [
                    [120, 67],
                    [120, 90],
                    [160, 90],
                    [160, 120],
                    [210, 118],
                    [320, 180],
                    [374, 210],
                    [320, 240],
                    [460, 260],
                    [480, 270],
                    [622, 350],
                    [480, 360],
                    [640, 360],
                    [640, 480],
                    [960, 540],
                    [1280, 720]])
            args['uses_bc_thumbnail_api'] = True

        integration = yield IntegrationHelper.create_integration(
            acct,
            args,
            neondata.IntegrationType.BRIGHTCOVE,
            cdn=cdn)

        statemon.state.increment('post_brightcove_oks')
        rv = yield self.db2api(integration)
        self.success(rv)

    @tornado.gen.coroutine
    def get(self, account_id):
        """handles a brightcove endpoint get request"""

        schema = Schema({
            Required('account_id') : All(Coerce(str),
                Length(min=1, max=256)),
            Required('integration_id') : All(Coerce(str),
                Length(min=1, max=256)),
            'fields': Any(CustomVoluptuousTypes.CommaSeparatedList())
        })
        args = self.parse_args()
        args['account_id'] = account_id = str(account_id)
        schema(args)

        fields = args.get('fields', None)
        if fields:
            fields = set(fields.split(','))

        integration_id = args['integration_id']
        integration = yield IntegrationHelper.get_integration(
            integration_id,
            neondata.IntegrationType.BRIGHTCOVE)
        statemon.state.increment('get_brightcove_oks')
        rv = yield self.db2api(integration, fields=fields)
        self.success(rv)

    @tornado.gen.coroutine
    def put(self, account_id):
        """handles a brightcove endpoint put request"""

        schema = Schema({
            Required('account_id') : All(Coerce(str), Length(min=1, max=256)),
            Required('integration_id') : All(Coerce(str), Length(min=1, max=256)),
            'read_token': All(Coerce(str), Length(min=1, max=1024)),
            'write_token': All(Coerce(str), Length(min=1, max=1024)),
            'application_client_id': All(Coerce(str), Length(min=1, max=1024)),
            'application_client_secret': All(Coerce(str), Length(min=1, max=1024)),
            'callback_url': All(Coerce(str), Length(min=1, max=1024)),
            'publisher_id': All(Coerce(str), Length(min=1, max=512)),
            'playlist_feed_ids': All(CustomVoluptuousTypes.CommaSeparatedList()),
            'uses_batch_provisioning': Boolean(),
            'uses_bc_thumbnail_api': Boolean(),
            'uses_bc_videojs_player': Boolean(),
            'uses_bc_smart_player': Boolean()
        })
        args = self.parse_args()
        args['account_id'] = account_id = str(account_id)
        integration_id = args['integration_id']
        schema(args)

        integration = yield IntegrationHelper.get_integration(
            integration_id,
            neondata.IntegrationType.BRIGHTCOVE)

        # Check credentials with Brightcove's CMS API.
        app_id = args.get('application_client_id', None)
        app_secret = args.get('application_client_secret', None)
        if app_id and app_secret:
            IntegrationHelper.validate_oauth_credentials(
                app_id,
                app_secret,
                neondata.IntegrationType.BRIGHTCOVE)

            # just run a basic search to see that the creds are ok
            lpd = yield self._get_last_processed_date(
                integration.publisher_id,
                app_id,
                app_secret)

            if not lpd:
                raise BadRequestError('Brightcove credentials are bad, ' \
                    'application_id or application_secret are wrong.')

        def _update_integration(p):
            p.read_token = args.get('read_token', integration.read_token)
            p.write_token = args.get('write_token', integration.write_token)
            p.application_client_id = app_id or \
                integration.application_client_id
            p.application_client_secret= app_secret or \
                integration.application_client_secret
            p.publisher_id = args.get('publisher_id', integration.publisher_id)
            playlist_feed_ids = args.get('playlist_feed_ids', None)
            if playlist_feed_ids:
                p.playlist_feed_ids = playlist_feed_ids.split(',')
            p.uses_batch_provisioning = Boolean()(
                args.get('uses_batch_provisioning',
                integration.uses_batch_provisioning))
            p.uses_bc_thumbnail_api = Boolean()(
                args.get('uses_bc_thumbnail_api',
                integration.uses_bc_thumbnail_api))
            p.uses_bc_videojs_player = Boolean()(
                args.get('uses_bc_videojs_player',
                integration.uses_bc_videojs_player))
            p.uses_bc_smart_player = Boolean()(
                args.get('uses_bc_smart_player',
                integration.uses_bc_smart_player))

        yield neondata.BrightcoveIntegration.modify(
            integration_id, _update_integration, async=True)

        integration = yield IntegrationHelper.get_integration(
            integration_id,
            neondata.IntegrationType.BRIGHTCOVE)

        statemon.state.increment('put_brightcove_oks')
        rv = yield self.db2api(integration)
        self.success(rv)

    @tornado.gen.coroutine
    def _get_last_processed_date(self, publisher_id, app_id, app_secret):
        """calls out to brightcove with the sent in app_id
             and app_secret to get the 4th most recent video
             so that we can set a reasonable last_process_date
             on this video

           raises on unknown exceptions
           returns none if a video search could not be completed
        """
        rv = None

        bc_cms_api = api.brightcove_api.CMSAPI(
            publisher_id,
            app_id,
            app_secret)
        try:
            # return the fourth oldest video
            videos = yield bc_cms_api.get_videos(
                limit=1,
                offset=3,
                sort='-updated_at')

            if videos and len(videos) is not 0:
                video = videos[0]
                rv = video['updated_at']
            else:
                rv = datetime.utcnow().strftime(
                    '%Y-%m-%dT%H:%M:%SZ')
        except (api.brightcove_api.BrightcoveApiServerError,
                api.brightcove_api.BrightcoveApiClientError,
                api.brightcove_api.BrightcoveApiNotAuthorizedError,
                api.brightcove_api.BrightcoveApiError) as e:
            _log.error('Brightcove Error occurred trying to get \
                        last_processed_date : %s' % e)
            pass
        except Exception as e:
            _log.error('Unknown Error occurred trying to get \
                        last_processed_date: %s' % e)
            raise

        raise tornado.gen.Return(rv)

    @classmethod
    def _get_default_returned_fields(cls):
        return [ 'integration_id', 'account_id', 'read_token',
                 'write_token', 'last_process_date', 'application_client_id',
                 'application_client_secret', 'publisher_id', 'callback_url',
                 'enabled', 'playlist_feed_ids', 'uses_batch_provisioning',
                 'uses_bc_thumbnail_api', 'uses_bc_videojs_player',
                 'uses_bc_smart_player', 'uses_bc_gallery', 'id_field',
                 'created', 'updated' ]

    @classmethod
    def _get_passthrough_fields(cls):
        return [ 'integration_id', 'account_id', 'read_token',
                 'write_token', 'last_process_date', 'application_client_id',
                 'application_client_secret', 'publisher_id', 'callback_url',
                 'enabled', 'playlist_feed_ids', 'uses_batch_provisioning',
                 'uses_bc_thumbnail_api', 'uses_bc_videojs_player',
                 'uses_bc_smart_player', 'uses_bc_gallery', 'id_field',
                 'created', 'updated' ]

    @classmethod
    def get_access_levels(self):
        return {
                 HTTPVerbs.GET: neondata.AccessLevels.READ,
                 HTTPVerbs.POST: neondata.AccessLevels.CREATE,
                 HTTPVerbs.PUT: neondata.AccessLevels.UPDATE,
                 'account_required': [HTTPVerbs.GET,
                                        HTTPVerbs.PUT,
                                        HTTPVerbs.POST]
               }


class ThumbnailAuth(object):
    """Mixin for checking if thumbnails keys are authorized"""

    def _authorize_thumb_ids_or_raise(self, tids):
        """Check format of thumbnail id against request ids"""

        if type(tids) is not list:
            tids = [tids]

        content_id = None
        content_type = None
        if self.share_payload:
            content_id = self.share_payload['content_id']
            content_type = self.share_payload['content_type']

        for tid in tids:
            try:
                tid_int_vid = neondata.InternalVideoID.from_thumbnail_id(tid)
                tid_acct_id = neondata.ThumbnailMetadata.get_account_id_from_tid(tid)
            except ValueError:
                raise ForbiddenError()
            if tid_acct_id != self.account_id:
                raise ForbiddenError()
            if content_id and content_type == 'VideoMetadata' and tid_int_vid != content_id:
                raise ForbiddenError()

    def _authorize_thumbs_or_raise(self, thumbs):
        """Check ids in thumbnail object against request ids"""

        if type(thumbs) is not list:
            thumbs = [thumbs]

        share_video_id = None
        if self.share_payload and self.share_payload['content_type'] == 'VideoMetadata':
            share_video_id = self.share_payload['content_id']

        for thumb in thumbs:
            if thumb.get_account_id() != self.account_id:
                raise ForbiddenError()
            if share_video_id and thumb.video_id != share_video_id:
                raise ForbiddenError()


class TagResponse(object):

    @staticmethod
    def _get_default_returned_fields():
        return ['tag_id', 'name', 'account_id', 'video_id', 'tag_type', 'created', 'updated', 'thumbnail_ids']

    @staticmethod
    def _get_passthrough_fields():
        return ['name', 'account_id', 'tag_type', 'created', 'updated']

    @staticmethod
    @tornado.gen.coroutine
    def _convert_special_field(obj, field):
        if field == 'tag_id':
            raise tornado.gen.Return(obj.get_id())
        if field == 'video_id':
            raise tornado.gen.Return(
                neondata.InternalVideoID.to_external(obj.video_id) if obj.video_id else None)
        if field == 'thumbnail_ids':
            ids = yield neondata.TagThumbnail.get(tag_id=obj.get_id(), async=True)

            raise tornado.gen.Return(ids)
        raise BadRequestError('invalid field %s' % field)

class TagAuth(object):

    def _authorize_tags_or_raise(self, tags):
        """Check tag's account id against request"""

        if type(tags) is not list:
            tags = [tags]

        if any([t for t in tags if t and t.account_id != self.account_id]):
            raise ForbiddenError()


'''*********************************************************************
TagHandler
*********************************************************************'''
class TagHandler(TagResponse, TagAuth, ThumbnailAuth, ShareableContentHandler):

    @tornado.gen.coroutine
    def post(self, account_id):
        Schema({
            Required('account_id'): All(Coerce(str), Length(min=1, max=256)),
            Required('name'): All(Coerce(unicode), Length(min=1, max=256)),
            'thumbnail_ids': Any(CustomVoluptuousTypes.CommaSeparatedList()),
            'type': CustomVoluptuousTypes.TagType()
        })(self.args)

        tag_type = self.args.get('type', TagType.COLLECTION)
        tag = neondata.Tag(
            None,
            account_id=self.args['account_id'],
            name=self.args['name'],
            tag_type=tag_type)
        yield tag.save(async=True)

        # Validate and save thumbnail associations.
        _thumb_ids = self.args.get('thumbnail_ids')
        _thumb_ids = _thumb_ids.split(',') if _thumb_ids else []
        self._authorize_thumb_ids_or_raise(_thumb_ids)
        thumb_ids = yield self._set_thumb_ids(tag, _thumb_ids)

        result = yield self.db2api(tag)
        self.success(result)

    @tornado.gen.coroutine
    def get(self, account_id):
        self.args = Schema({
            Required('account_id'): All(Coerce(str), Length(min=1, max=256)),
            Required('tag_id'): Any(CustomVoluptuousTypes.CommaSeparatedList()),
            'fields': Any(CustomVoluptuousTypes.CommaSeparatedList())
        })(self.args)

        # Ensure tags are valid and permitted.
        tag_ids = self.args['tag_id']

        # Check share permission.
        self._allow_request_by_share_or_raise(tag_ids, neondata.Tag.__name__)

        account_id = self.args['account_id']
        _tags = yield neondata.Tag.get_many(tag_ids, async=True)
        tags = [t for t in _tags if t]
        self._authorize_tags_or_raise(tags)

        # Get dict of tag id to list of thumb id.
        fields = self.args.get('fields')
        result = yield {tag.get_id(): self.db2api(tag, fields) for tag in tags if tag}
        self.success(result)

    @tornado.gen.coroutine
    def put(self, account_id):
        Schema({
            Required('account_id'): All(Coerce(str), Length(min=1, max=256)),
            Required('tag_id'): Any(CustomVoluptuousTypes.CommaSeparatedList()),
            'thumbnail_ids': Any(CustomVoluptuousTypes.CommaSeparatedList()),
            'name': All(Coerce(unicode), Length(min=1, max=256)),
            'type': CustomVoluptuousTypes.TagType(),
            'hidden': Boolean()
        })(self.args)

        # Validate.
        _thumb_ids = self.args.get('thumbnail_ids')
        thumb_ids = _thumb_ids.split(',') if _thumb_ids else []
        self._authorize_thumb_ids_or_raise(thumb_ids)

        # Update the tag itself.
        def _update(tag):
            if self.args['account_id'] != tag.account_id:
                raise ForbiddenError()
            if self.args.get('name'):
                tag.name = self.args['name']
            if self.args.get('tag_type'):
                tag.tag_type = self.args['tag_type']
            tag.hidden = Boolean()(self.args.get('hidden', tag.hidden));
        tag = yield neondata.Tag.modify(
            self.args['tag_id'],
            _update,
            async=True)

        # Save associations.
        yield self._set_thumb_ids(tag, thumb_ids)

        result = yield self.db2api(tag)
        self.success(result)

    @tornado.gen.coroutine
    def delete(self, account_id):
        Schema({
            Required('account_id'): All(Coerce(str), Length(min=1, max=256)),
            Required('tag_id'): Any(CustomVoluptuousTypes.CommaSeparatedList()),
        })(self.args)

        tag = yield neondata.Tag.get(self.args['tag_id'], async=True)
        self._authorize_tags_or_raise(tag)

        if not tag:
            raise NotFoundError('That tag is not found')

        # Delete the tag.
        yield neondata.Tag.delete(tag.get_id(), async=True)
        self.success({'tag_id': tag.get_id()})

    @tornado.gen.coroutine
    def _set_thumb_ids(self, tag, thumb_ids):
        '''Add the thumb ids to the tag in the database and return all'''
        _thumbs = yield neondata.ThumbnailMetadata.get_many(thumb_ids, async=True)
        thumbs = [t for t in _thumbs if t]
        if thumbs:
            if(any([th.get_account_id() != tag.account_id for th in thumbs])):
                raise ForbiddenError
            valid_thumb_ids = [thumb.get_id() for thumb in thumbs]
            if valid_thumb_ids:
                result = yield neondata.TagThumbnail.save_many(
                    tag_id=tag.get_id(),
                    thumbnail_id=valid_thumb_ids,
                    async=True)
        # Get the current list of thumbnails.
        thumb_ids = yield neondata.TagThumbnail.get(tag_id=tag.get_id(), async=True)
        raise tornado.gen.Return(thumb_ids)

    @classmethod
    def get_access_levels(self):
        return {
            HTTPVerbs.GET: neondata.AccessLevels.READ,
            HTTPVerbs.POST: neondata.AccessLevels.CREATE,
            HTTPVerbs.PUT: neondata.AccessLevels.UPDATE,
            HTTPVerbs.DELETE: neondata.AccessLevels.DELETE,
            'account_required': [HTTPVerbs.GET,
                                 HTTPVerbs.PUT,
                                 HTTPVerbs.POST]}


'''*********************************************************************
TagSearchExternalHandler : class responsible for searching tags
                           from an external source
   HTTP Verbs     : get
*********************************************************************'''
class TagSearchExternalHandler(TagResponse, APIV2Handler):

    @tornado.gen.coroutine
    def get(self, account_id):
        self.args = Schema({
            Required('account_id'): All(Coerce(str), Length(min=1, max=256)),
            Optional('limit', default=25): All(Coerce(int), Range(min=1, max=100)),
            'query': str,
            'fields': CustomVoluptuousTypes.CommaSeparatedList(),
            'since': Coerce(float),
            'until': Coerce(float),
            'show_hidden': Coerce(bool),
            'tag_type': CustomVoluptuousTypes.TagType()
        })(self.args)
        self.args['base_url'] = '/api/v2/%s/tags/search/' % self.account_id
        searcher = ContentSearcher(**self.args)

        _tags, count, prev_page, next_page = yield searcher.get()
        _fields = self.args.get('fields')
        fields = _fields.split(',') if _fields else None
        tags = yield [self.db2api(t, fields) for t in _tags]

        self.success({
            'items': tags,
            'count': count,
            'next_page': next_page,
            'prev_page': prev_page})

    @staticmethod
    def get_access_levels():
        return {
            HTTPVerbs.GET: neondata.AccessLevels.READ,
            'account_required': [HTTPVerbs.GET]}


class ContentSearcher(object):
    '''A searcher to run search requests and make results.'''

    def __init__(self, account_id=None, since=None, until=None, query=None,
                 fields=None, limit=None, show_hidden=False, base_url=None,
                 tag_type=None):
        self.account_id = account_id
        self.since = since or 0.0
        self.until = until or 0.0
        self.query = query
        self.fields = fields
        self.limit = limit
        self.show_hidden = show_hidden
        self.base_url = base_url or '/api/v2/tags/search/'
        self.tag_type = tag_type

    @tornado.gen.coroutine
    def get(self):
        '''Gets a search result tuple.

        Returns tuple of
            list of content items,
            int count of items in this response,
            str prev page url,
            str next page url.'''
        args = {k:v for k,v in self.__dict__.items() if k not in ['base_url', 'fields']}
        args['async'] = True
        tags, min_time, max_time = yield neondata.Tag.search_for_objects_and_times(**args)
        raise tornado.gen.Return((
            tags,
            len(tags),
            self._prev_page_url(min_time),
            self._next_page_url(max_time)))

    def _prev_page_url(self, timestamp):
        '''Build the previous page url.'''
        return self._page_url('since', timestamp)

    def _next_page_url(self, timestamp):
        '''Build the previous page url.'''
        return self._page_url('until', timestamp)

    def _page_url(self, time_type, timestamp):
        return '{base}?{time_type}={ts}&limit={limit}{query}{fields}{acct}'.format(
            base=self.base_url,
            time_type=time_type,
            ts=timestamp,
            limit=self.limit,
            query='&query=%s' % self.query if self.query else '',
            fields='&fields=%s' % ','.join(self.fields) if self.fields else '',
            acct='&account_id=%s' % self.account_id if self.account_id else '')


'''*********************************************************************
TagSearchInternalHandler : class responsible for searching tags
                           from an internal source
   HTTP Verbs     : get
*********************************************************************'''
class TagSearchInternalHandler(APIV2Handler):
    @tornado.gen.coroutine
    def get(self):
        self.args = Schema({
            'account_id': All(Coerce(str), Length(min=1, max=256)),
            Optional('limit', default=25): All(Coerce(int), Range(min=1, max=100)),
            'query': str,
            'since': All(Coerce(float)),
            'until': All(Coerce(float)),
            'show_hidden': Coerce(bool),
            'fields': CustomVoluptuousTypes.CommaSeparatedList(),
            'tag_type': CustomVoluptuousTypes.TagType()
        })(self.args)

        self.args['base_url'] = '/api/v2/tags/search/'
        searcher = ContentSearcher(**self.args)

        _tags, count, prev_page, next_page = yield searcher.get()
        fields = self.args.get('fields')
        tags = yield [self.db2api(t, fields) for t in _tags]

        self.success({
            'items': tags,
            'count': count,
            'next_page': next_page,
            'prev_page': prev_page})

    @classmethod
    def get_access_levels(self):
        return {
            HTTPVerbs.GET: neondata.AccessLevels.READ,
            'internal_only': True}


'''*********************************************************************
ThumbnailHandler
*********************************************************************'''
class ThumbnailHandler(ThumbnailAuth, TagAuth, ShareableContentHandler):

    def initialize(self):
        super(ThumbnailHandler, self).initialize()
        self.predictor = None
        self.imagePrep = utils.pycvutils.ImagePrep(convert_to_color=True)

    @tornado.gen.coroutine
    def post(self, account_id):
        """Create a new thumbnail"""

        # The client can submit either a url argument or file in the body
        # with a Content-Type: multipart/form-data header.
        schema = Schema({
            Required('account_id') : All(Coerce(str), Length(min=1, max=256)),
            # Video id associates this image as thumbnail of a video.
            'video_id' : All(Coerce(str), Length(min=1, max=256)),
            'url': All(CustomVoluptuousTypes.CommaSeparatedList(), 
              Coerce(str), 
              Length(min=1)),
            # Tag id associates the image with collection(s).
            'tag_id': All(CustomVoluptuousTypes.CommaSeparatedList(), 
              Coerce(str), 
              Length(min=1)),
            # This is a partner's id for the image.
            'thumbnail_ref' : All(Coerce(str), Length(min=1, max=1024)),
            # fields wanted to be returned on this POST call, 
            # here for features (which is huge) 
            'fields': Any(CustomVoluptuousTypes.CommaSeparatedList())
        })
        self.args = self.parse_args()
        self.args['account_id'] = account_id
        schema(self.args)

        fields = self.args.get('fields', None)
        if fields:
            fields = set(fields.split(','))

        # Ensure tags are valid and permitted.
        tag_ids = self.args.get('tag_id', '').split(',')
        account_id = self.args['account_id']
        _tags = yield neondata.Tag.get_many(tag_ids, async=True)
        self.tags = [t for t in _tags if t]
        self._authorize_tags_or_raise(self.tags)

        self.video = None
        self.images = []  # 2-ple of (url or None, PIL image)
        self.thumbs = []  # Thumbnailmetadata

        # Switch on whether a video is tied to this submission.
        if self.args.get('video_id'):
            yield self._post_with_video(fields)
            return
        yield self._post_without_video(fields)

    @tornado.gen.coroutine
    def _post_with_video(self, fields=None):
        """Set image and thumbnail data object with video association.

        Confirm video exists, then add the thumbnail to the video's
        list of thumbnails. Calculate the new thumbnail's rank from the old
        thumbnails."""
        _video_id = neondata.InternalVideoID.generate(
            self.account_id, self.args['video_id'])
        self.video = yield neondata.VideoMetadata.get(
            _video_id,
            async=True)
        if not self.video:
            raise NotFoundError('No video for {}'.format(_video_id))
        thumbs = yield neondata.ThumbnailMetadata.get_many(
            self.video.thumbnail_ids,
            async=True)

        # Calculate new thumbnails' rank: one less than everything else
        # or default value 0 if no other thumbnail.
        existing_thumbs = [t.rank for t in thumbs
                           if t.type == neondata.ThumbnailType.CUSTOMUPLOAD]
        rank = min(existing_thumbs) - 1 if existing_thumbs else 0

        # Save the image files and thumbnail data objects.
        yield self._set_thumbs(rank)

        statemon.state.increment('post_thumbnail_oks')
        yield self._respond_with_thumbs(fields)

    @tornado.gen.coroutine
    def _post_without_video(self, fields=None):
        """Set images to CDN. Set the thumb data to database.

        Returns- the new thumbnail."""
        yield self._set_thumbs()
        statemon.state.increment('post_thumbnail_oks')
        yield self._respond_with_thumbs(fields)

    @tornado.gen.coroutine
    def _set_thumbs(self, rank=None):
        """Set self.thumb to a new thumbnail from submitted image."""

        # Set self.images.
        yield self._load_images_from_request()

        # Build common objects for all thubmails.
        if self.video:
            video_id = self.video.get_id()
            integration_id = self.video.integration_id
        else:
            video_id = neondata.InternalVideoID.generate(self.account_id)
            integration_id = None
        # Get CDN store.
        cdn = yield neondata.CDNHostingMetadataList.get(
            neondata.CDNHostingMetadataList.create_key(
                self.account_id,
                integration_id),
            async=True)

        for (url, image) in self.images:

            # Instantiate a thumbnail object.
            _thumb = neondata.ThumbnailMetadata(
                None,
                internal_vid=video_id,
                external_id=self.args.get('thumbnail_ref'),
                ttype=neondata.ThumbnailType.CUSTOMUPLOAD,
                rank=rank)

            # If the thumbnail is tied to a video, set that association.
            if self.video:
                _thumb = yield self.video.download_and_add_thumbnail(
                    _thumb,
                    image=image,
                    image_url=url,
                    cdn_metadata=cdn,
                    save_objects=True,
                    async=True)
            else:
                yield _thumb.add_image_data(
                    image,
                    cdn_metadata=cdn,
                    async=True)
                yield _thumb.save(async=True)

            self.thumbs.append(_thumb)

        yield self._score_images()

        # Set tags if requested.
        if self.tags:
            yield neondata.TagThumbnail.save_many(
                tag_id=[t.get_id() for t in self.tags],
                thumbnail_id=[t.get_id() for t in self.thumbs],
                async=True)

    @tornado.gen.coroutine
    def _load_images_from_request(self):
        """Sets self.images to PIL images from request urls or multipart body.

        Handles mix of submission format. Will set self.images to at least one
        image or raise 400 on the batch"""

        # Get each from urls.
        _urls = self.args.get('url', '').split(',')
        urls = [u.strip() for u in _urls if u]
        for url in urls:
            _image = yield neondata.ThumbnailMetadata.download_image_from_url(url, async=True)
            self.images.append((url, _image))

        # Get each in body.
        for fl in self.request.files.itervalues():
            for upload in fl:
                try:
                    _image = ThumbnailHandler._get_image_from_httpfile(
                        upload)
                    self.images.append((None, _image))
                except IOError as e:
                    _log.warn('Could not get image from request body')
                    pass

        # If all are bad, raise a 400.
        if not self.images:
            raise BadRequestError('No image available',
                                  ResponseCode.HTTP_BAD_REQUEST)

    @staticmethod
    def _get_image_from_httpfile(httpfile):
        """Get the image from the http post request.
           Inputs- a HTTPFile, or any dict with body string
           Returns- instance of PIL.Image
        """
        return PIL.Image.open(io.BytesIO(httpfile.body))

    @tornado.gen.coroutine
    def _score_images(self):
        self._initialize_predictor()
        # Convert from PIL ImageFile to well-formatted cv2 for predict.
        for (_, i), t in (zip(self.images, self.thumbs)):
            cv_image = self.imagePrep(i)
            yield t.score_image(self.predictor, cv_image, True)

    @tornado.gen.coroutine
    def _respond_with_thumbs(self, fields=None):
        """Success. Reload the thumbnail and return it."""
        rv = yield [self.db2api(t, fields=fields) for t in self.thumbs]
        self.success({'thumbnails': rv}, code=ResponseCode.HTTP_ACCEPTED)

    @tornado.gen.coroutine
    def put(self, account_id):
        """handles a thumbnail endpoint put request"""

        schema = Schema({
          Required('account_id'): Any(str, unicode, Length(min=1, max=256)),
          Required('thumbnail_id'): Any(str, unicode, Length(min=1, max=512)),
          'enabled': Boolean()
        })
        args = self.parse_args()
        args['account_id'] = str(account_id)
        schema(args)
        thumbnail_id = args['thumbnail_id']

        def _update_thumbnail(t):
            t.enabled = Boolean()(args.get('enabled', t.enabled))

        thumbnail = yield tornado.gen.Task(neondata.ThumbnailMetadata.modify,
                                           thumbnail_id,
                                           _update_thumbnail)

        statemon.state.increment('put_thumbnail_oks')
        thumbnail = yield self.db2api(thumbnail)
        self.success(thumbnail)

    @tornado.gen.coroutine
    def get(self, account_id):
        """handles a thumbnail endpoint get request"""

        schema = Schema({
            Required('account_id'): Any(str, unicode, Length(min=1, max=256)),
            Required('thumbnail_id'): Any(CustomVoluptuousTypes.CommaSeparatedList()),
            'fields': Any(CustomVoluptuousTypes.CommaSeparatedList()),
            'gender': In(['M', 'F', None]),
            'age': In(['18-19', '20-29', '30-39', '40-49', '50+', None])})

        args = self.parse_args()
        args['account_id'] = str(account_id)
        schema(args)

        query_tids = args['thumbnail_id'].split(',')

        self._authorize_thumb_ids_or_raise(query_tids)

        fields = args.get('fields', None)
        if fields:
            fields = set(fields.split(','))
        gender = args.get('gender', None)
        age = args.get('age', None)


        _thumbs = yield neondata.ThumbnailMetadata.get_many(
            query_tids,
            async=True)
        thumbs = [t for t in _thumbs if t]

        self._authorize_thumbs_or_raise(thumbs)

        thumbnails = yield [
            ThumbnailHandler.db2api(
                t,
                gender=gender,
                age=age,
                fields=fields)
            for t in thumbs]

        if not thumbnails:
            raise NotFoundError(
                'thumbnails do not exist with ids = %s' % (query_tids))

        rv = {
            'thumb_count': len(thumbnails),
            'thumbnails': thumbnails}
        statemon.state.increment('get_thumbnail_oks')
        self.success(rv)

    def _initialize_predictor(self):
        '''Instantiate and connect an Aquila predictor.'''

        # Check if model is already set.
        if self.predictor:
            return

        aquila_conn = utils.autoscale.MultipleAutoScaleGroups(
            options.model_autoscale_groups.split(','))
        self.predictor = model.predictor.DeepnetPredictor(
            port=options.model_server_port,
            concurrency=options.request_concurrency,
            aquila_connection=aquila_conn)
        self.predictor.connect()

    @classmethod
    def _get_default_returned_fields(cls):
        return ['video_id', 'thumbnail_id', 'rank', 'frameno', 'tag_ids',
                'neon_score', 'enabled', 'url', 'height', 'width',
                'type', 'external_ref', 'created', 'updated', 'renditions',
                'dominant_color']

    @classmethod
    def _get_passthrough_fields(cls):
        return ['rank', 'frameno', 'enabled', 'type', 'width', 'height',
                'created', 'updated', 'dominant_color']

    @classmethod
    @tornado.gen.coroutine
    def _convert_special_field(cls, obj, field, age=None, gender=None):
        if field == 'video_id':
            retval = neondata.InternalVideoID.to_external(
                neondata.InternalVideoID.from_thumbnail_id(obj.key))
        elif field == 'thumbnail_id':
            retval = obj.key
        elif field == 'tag_ids':
            tag_ids = yield neondata.TagThumbnail.get(
                thumbnail_id=obj.key,
                 async=True)
            retval = list(tag_ids)
        elif field == 'neon_score':
            retval = obj.get_neon_score(age=age, gender=gender)
        elif field == 'url':
            retval = obj.urls[0] if obj.urls else []
        elif field == 'external_ref':
            retval = obj.external_id
        elif field == 'renditions':
            urls = yield neondata.ThumbnailServingURLs.get(obj.key, async=True)
            retval = ThumbnailHelper.renditions_of(urls)
        elif field == 'feature_ids':
            retval = ThumbnailHelper.get_feature_ids(
                obj,
                age=age,
                gender=gender)
        elif field == 'features':
            retval = list(obj.features)
        else:
            raise BadRequestError('invalid field %s' % field)

        raise tornado.gen.Return(retval)

    def get_limits(self):
        '''Limit the post of images'''

        try:
            increment = len(self.images)
        except AttributeError:
            increment = 1
        post_list = [{ 'left_arg': 'image_posts',
                       'right_arg': 'max_image_posts',
                       'operator': '<',
                       'timer_info': {
                           'refresh_time': 'refresh_time_image_posts',
                           'add_to_refresh_time': 'seconds_to_refresh_image_posts',
                           'timer_resets': [ ('image_posts', 0) ]
                       },
                       'values_to_increase': [ ('image_posts', increment) ],
                       'values_to_decrease': []
        }]
        return {
                   HTTPVerbs.POST: post_list
               }

    @classmethod
    def get_access_levels(self):
        return {
            HTTPVerbs.GET: neondata.AccessLevels.READ,
            HTTPVerbs.POST: neondata.AccessLevels.CREATE,
            HTTPVerbs.PUT: neondata.AccessLevels.UPDATE,
            'account_required': [HTTPVerbs.PUT, HTTPVerbs.POST]}


'''*********************************************************************
ThumbnailHelper
*********************************************************************'''
class ThumbnailHelper(object):
    """A collection of stateless functions for working on Thumbnails"""

    @staticmethod
    @tornado.gen.coroutine
    def get_thumbnails(tids):
        """gets thumbnailmetadata objects

        Keyword arguments:
        tids -- a list of tids that needs to be retrieved
        """
        thumbnails = []
        if tids:
            thumbnails = yield tornado.gen.Task(
                neondata.ThumbnailMetadata.get_many,
                tids)
            thumbnails = yield [ThumbnailHandler.db2api(x) for
                                x in thumbnails]
            renditions = yield ThumbnailHelper.get_renditions(tids)
            for thumbnail in thumbnails:
                thumbnail['renditions'] = renditions[thumbnail['thumbnail_id']]

        raise tornado.gen.Return(thumbnails)

    @staticmethod
    @tornado.gen.coroutine
    def get_renditions(tids):
        """Given list of thumbnails ids, get all renditions as map of tid.

        Input- list of thumbnail ids
        Yields- [ tid0: [rendition1, .. renditionN], tid1: [...], ...}
            where rendition has format {
                'url': string
                'width': int,
                'height': int,
                'aspect_ratio': string in format "WxH"
        """
        urls = yield neondata.ThumbnailServingURLs.get_many(tids, async=True)
        # Build a map of {tid: [renditions]}.
        rv = {}
        for chunk in urls:
            if chunk:
                renditions = [ThumbnailHelper._to_dict(pair) for pair in chunk]
                try:
                    rv[chunk.get_id()].extend(renditions)
                except KeyError:
                    rv[chunk.get_id()] = renditions
        # Ensure that every tid in request has a list mapped.
        for tid in tids:
            if not rv.get(tid):
                rv[tid] = []
        raise tornado.gen.Return(rv)

    @staticmethod 
    def get_feature_ids(obj, gender=None, age=None): 
        if not obj.features: 
            return None 
        if not obj.model_version: 
            return None
        model_name = obj.model_version
        predictor = model.predictor.DemographicSignatures(obj.model_version)
        importance = predictor.compute_feature_importance(obj.features,
                                                          gender, age)
        return [(neondata.Feature.create_key(model_name, idx), val)
                 for idx, val in importance.iteritems()]

    @staticmethod
    def renditions_of(urls_obj):
        """Given a ThumbnailServingURLs, get a list of rendition dicts.

        Input- urls_obj a ThumbnailServingURLs
        Returns- list of rendition dictionaries
            i.e., [rendition1, rendition2, ... , renditionN]
            where rendition has format {
                'url': string
                'width': int,
                'height': int,
                'aspect_ratio': string in format "WxH"
        """
        return [ThumbnailHelper._to_dict(item) for item in urls_obj] if urls_obj else []


    @staticmethod
    def _to_dict(pair):
        """Given a size map (sizes, url) tuple return a rendition dictionary."""
        dimensions, url = pair

        return {
            'url': url,
            'width': dimensions[0],
            'height': dimensions[1],
            'aspect_ratio': '%sx%s' % ThumbnailHelper._get_ar(*dimensions)}

    @staticmethod
    def _get_ar(width, height):
        """Calculate aspect ratio from width, height."""
        f = fractions.Fraction(width, height)
        if f.numerator == 120 and f.denominator == 67:
            return 16, 9
        return f.numerator, f.denominator


'''*********************************************************************
VideoHelper
*********************************************************************'''
class VideoHelper(object):
    """helper class designed to help the video endpoint handle requests"""
    @staticmethod
    @tornado.gen.coroutine
    def create_api_request(args, account_id_api_key):
        """creates an API Request object

        Keyword arguments:
        args -- the args sent to the api endpoint
        account_id_api_key -- the account_id/api_key
        """
        job_id = uuid.uuid1().hex
        integration_id = args.get('integration_id')

        request = neondata.NeonApiRequest(job_id, api_key=account_id_api_key)
        request.video_id = args['external_video_ref']
        if integration_id:
            request.integration_id = integration_id
        request.video_url = args.get('url')
        request.callback_url = args.get('callback_url')
        request.video_title = args.get('title')
        request.default_thumbnail = args.get('default_thumbnail_url')
        request.external_thumbnail_ref = args.get('thumbnail_ref')
        request.publish_date = args.get('publish_date')
        request.callback_email = args.get('callback_email')
        request.age = args.get('age')
        request.gender = args.get('gender')
        request.default_clip = args.get('default_clip_url')

        # set the requests result type
        result_type = args.get('result_type')
        if result_type and result_type.lower() == neondata.ResultType.CLIPS:
            request.n_clips = int(args.get('n_clips', 1))
            request.result_type = result_type
            request.clip_length = args.get('clip_length')
            if request.clip_length is not None:
                request.clip_length = float(request.clip_length)
        else:
            request.result_type = neondata.ResultType.THUMBNAILS
            request.api_param = int(args.get('n_thumbs', 5))

        yield request.save(async=True)

        if request:
            raise tornado.gen.Return(request)

    @staticmethod
    @tornado.gen.coroutine
    def create_video_and_request(args, account_id_api_key):
        """creates Video object and ApiRequest object and
           sends them back to the caller as a tuple

        Keyword arguments:
        args -- the args sent to the api endpoint
        account_id_api_key -- the account_id/api_key
        """

        video_id = args['external_video_ref']
        internal_video_id = neondata.InternalVideoID.generate(
            account_id_api_key,
            video_id)
        video = yield neondata.VideoMetadata.get(
            internal_video_id,
            async=True)
        if video is None:
            # Generate share token.
            share_payload = {
                'content_type': 'VideoMetadata',
                'content_id': internal_video_id
            }

            duration = args.get('duration', None)
            if duration:
                duration=float(duration)

            video = neondata.VideoMetadata(
                internal_video_id,
                video_url=args.get('url', None),
                publish_date=args.get('publish_date', None),
                duration=duration,
                custom_data=args.get('custom_data', None),
                i_id=args.get('integration_id', '0'),
                serving_enabled=False)

            default_thumbnail_url = args.get('default_thumbnail_url', None)
            if default_thumbnail_url:
                # save the default thumbnail
                thumb = yield video.download_and_add_thumbnail(
                    image_url=default_thumbnail_url,
                    external_thumbnail_id=args.get('thumbnail_ref', None),
                    async=True)
                # bypassing save_objects to avoid the extra video save
                # that comes later
                success = yield thumb.save(async=True) 
                if not success: 
                    raise IOError('unable to save default thumbnail')

            # create the api_request
            api_request = yield VideoHelper.create_api_request(
                args,
                account_id_api_key)

            # Create a Tag for this video.
            tag = neondata.Tag(
                account_id=account_id_api_key,
                video_id=internal_video_id,
                tag_type='video',
                name=api_request.video_title)
            yield tag.save(async=True)
            video.tag_id = tag.get_id()

            # add the job id save the video
            video.job_id = api_request.job_id
            yield video.save(async=True)
            raise tornado.gen.Return((video, api_request))
        else:
            reprocess = Boolean()(args.get('reprocess', False))
            if reprocess:
                # Flag the request to be reprocessed
                def _flag_reprocess(x):
                    if x.state in [neondata.RequestState.SUBMIT,
                                   neondata.RequestState.REPROCESS,
                                   neondata.RequestState.REQUEUED,
                                   neondata.RequestState.PROCESSING,
                                   neondata.RequestState.FINALIZING]:
                        raise AlreadyExists(
                            'A job for this video is currently underway. '
                            'Please try again later')
                    x.state = neondata.RequestState.REPROCESS
                    x.fail_count = 0
                    x.try_count = 0
                    x.response = {}
                    x.age = args.get('age', None)
                    x.gender = args.get('gender', None)

                    x.result_type = args.get(
                        'result_type',
                        x.result_type).lower()
                    x.n_clips = args.get('n_clips', x.n_clips)
                    if x.n_clips is not None:
                        x.n_clips = int(x.n_clips)
                    x.clip_length = args.get('clip_length', x.clip_length)
                    if x.clip_length is not None:
                        x.clip_length = float(x.clip_length)
                    x.api_param = args.get('n_thumbs', x.api_param)
                    if x.api_param is not None:
                        x.api_param = int(x.api_param)
                api_request = yield neondata.NeonApiRequest.modify(
                    video.job_id,
                    account_id_api_key,
                    _flag_reprocess,
                    async=True)

                raise tornado.gen.Return((video, api_request))
            else:
                raise AlreadyExists('This item already exists: job_id=%s' % (video.job_id))

    @staticmethod
    @tornado.gen.coroutine
    def get_thumbnails_from_ids(tids, gender=None, age=None):
        """gets thumbnailmetadata objects

        Keyword arguments:
        tids -- a list of tids that needs to be retrieved
        gender - A gender to get the thumbnail data for 
        age - An age group to get the thumbnail data for
        """
        thumbnails = []
        if tids:
            tids = set(tids)
            thumbnails = yield tornado.gen.Task(
                neondata.ThumbnailMetadata.get_many,
                tids)
            thumbnails = yield [ThumbnailHandler.db2api(x, gender=gender,
                                                        age=age) for
                                x in thumbnails]
            renditions = yield ThumbnailHelper.get_renditions(tids)
            for thumbnail, tid in zip(*(thumbnails, tids)):
                thumbnail['renditions'] = renditions[tid]

        raise tornado.gen.Return(thumbnails)

    @staticmethod
    @tornado.gen.coroutine
    def get_search_results(account_id=None, since=None, until=None,
                           query=None, limit=None, fields=None,
                           base_url='/api/v2/videos/search', show_hidden=False):

        videos, until_time, since_time = \
                yield neondata.VideoMetadata.search_for_objects_and_times(
            account_id=account_id,
            since=since,
            until=until,
            limit=limit,
            query=query,
            show_hidden=show_hidden,
            async=True)

        vid_dict = yield VideoHelper.build_response(videos, fields)

        vid_dict['next_page'] = VideoHelper.build_page_url(
            base_url,
            until_time if until_time else 0.0,
            limit=limit,
            page_type='until',
            query=query,
            fields=fields,
            account_id=account_id)
        vid_dict['prev_page'] = VideoHelper.build_page_url(
            base_url,
            since_time if since_time else 0.0,
            limit=limit,
            page_type='since',
            query=query,
            fields=fields,
            account_id=account_id)
        raise tornado.gen.Return(vid_dict)

    @staticmethod
    @tornado.gen.coroutine
    def build_response(videos, fields, video_ids=None):

        vid_dict = {}
        vid_dict['videos'] = None
        vid_dict['video_count'] = 0
        new_videos = []
        vid_counter = 0
        index = 0
        videos = [x for x in videos if x and x.job_id]
        job_ids = [(v.job_id, v.get_account_id()) for v in videos]

        requests = yield neondata.NeonApiRequest.get_many(job_ids, async=True)
        for video, request in zip(videos, requests):
            index += 1 
            if video is None or request is None:
                if video_ids: 
                    new_videos.append({'error': 'video does not exist',		
                        'video_id': video_ids[index-1] }) 
                continue

            new_video = yield VideoHelper.db2api(video,
                                                 request,
                                                 fields)
            new_videos.append(new_video)
            vid_counter += 1

        vid_dict['videos'] = new_videos
        vid_dict['video_count'] = vid_counter

        raise tornado.gen.Return(vid_dict)

    @staticmethod
    def build_page_url(base_url,
                       time_stamp,
                       limit,
                       page_type=None,
                       query=None,
                       fields=None,
                       account_id=None):
        next_page_url = '%s?%s=%f&limit=%d' % (
            base_url,
            page_type,
            time_stamp,
            limit)
        if query:
            next_page_url += '&query=%s' % query
        if fields:
            next_page_url += '&fields=%s' % \
                ",".join("{0}".format(f) for f in fields)
        if account_id:
            next_page_url += '&account_id=%s' % account_id

        return next_page_url

    @staticmethod
    def get_estimated_remaining(request):
        if request.time_remaining is None:
            return None

        updated_ts = dateutil.parser.parse(
            request.updated)
        utc_now = datetime.utcnow()
        diff = (utc_now - updated_ts).total_seconds()
 
        return max(float(request.time_remaining - diff), 0.0)

    @staticmethod
    @tornado.gen.coroutine
    def db2api(video, request, fields=None):
        """Converts a database video metadata object to a video
        response dictionary

        Overwrite the base function because we have to do a join on the request

        Keyword arguments:
        video - The VideoMetadata object
        request - The NeonApiRequest object
        fields - List of fields to return
        """
        if fields is None:
            fields = ['state', 'video_id', 'publish_date', 'title', 'url',
                      'testing_enabled', 'job_id', 'tag_id', 'estimated_time_remaining']

        new_video = {}
        for field in fields:
            if field == 'thumbnails':
                # Get the main thumbnails to return. Start with
                # thumbnail_ids being present, then fallback to
                # job_results default run
                main_tids = video.thumbnail_ids
                if not main_tids:
                    for video_result in video.job_results:
                        if (video_result.age is None and
                            video_result.gender is None):
                            main_tids = video_result.thumbnail_ids
                            break
                    if not main_tids and len(video.job_results) > 0:
                        main_tids = video.job_results[0].thumbnail_ids
                new_video['thumbnails'] = yield \
                  VideoHelper.get_thumbnails_from_ids(
                      (main_tids + video.non_job_thumb_ids))
            elif field == 'demographic_thumbnails':
                new_video['demographic_thumbnails'] = []
                for video_result in video.job_results:
                    cur_thumbs = yield VideoHelper.get_thumbnails_from_ids(
                        (video_result.thumbnail_ids + video.non_job_thumb_ids),
                        age=video_result.age,
                        gender=video_result.gender)
                    cur_entry = {
                        'gender': video_result.gender,
                        'age': video_result.age,
                        'thumbnails': cur_thumbs}
                    cur_entry['bad_thumbnails'] = yield \
                      VideoHelper.get_thumbnails_from_ids(
                          video_result.bad_thumbnail_ids,
                          age=video_result.age,
                          gender=video_result.gender)
                    new_video['demographic_thumbnails'].append(cur_entry)
                if (len(video.job_results) == 0 and
                    len(video.thumbnail_ids) > 0):
                    # For backwards compability create a demographic
                    # thumbnail entry that's generic demographics if
                    # the video is done processing.
                    cur_thumbs = yield VideoHelper.get_thumbnails_from_ids(
                        video.thumbnail_ids)
                    if (neondata.ThumbnailType.NEON in
                        [x['type'] for x in cur_thumbs]):
                        new_video['demographic_thumbnails'].append({
                            'gender' : None,
                            'age' : None,
                            'thumbnails' : cur_thumbs})
            elif field == 'bad_thumbnails':
                # demographic_thumbnails are also required here and
                # are handled in that section.
                pass
            elif field == 'demographic_clip_ids':
                new_video['demographic_clip_ids'] = \
                    VideoHelper.get_demographic_clip_ids(video)
            elif field == 'state':
                new_video[field] = neondata.ExternalRequestState.from_internal_state(request.state)
            elif field == 'integration_id':
                new_video[field] = video.integration_id
            elif field == 'testing_enabled':
                # TODO: maybe look at the account level abtest?
                new_video[field] = video.testing_enabled
            elif field == 'job_id':
                new_video[field] = video.job_id
            elif field == 'title':
                if request:
                    new_video[field] = request.video_title
            elif field == 'video_id':
                new_video[field] = \
                  neondata.InternalVideoID.to_external(video.key)
            elif field == 'serving_url':
                new_video[field] = video.serving_url
            elif field == 'publish_date':
                new_video[field] = request.publish_date
            elif field == 'duration':
                new_video[field] = video.duration
            elif field == 'custom_data':
                new_video[field] = video.custom_data
            elif field == 'created':
                new_video[field] = video.created
            elif field == 'updated':
                new_video[field] = video.updated
            elif field == 'url':
                new_video[field] = video.url
            elif field == 'tag_id':
                new_video[field] = video.tag_id
            elif field == 'estimated_time_remaining':
                new_video[field] = VideoHelper.get_estimated_remaining(
                    request)
            else:
                raise BadRequestError('invalid field %s' % field)

            if request:
                err = request.response.get('error', None)
                if err:
                    new_video['error'] = err

        raise tornado.gen.Return(new_video)

    @staticmethod
    def get_demographic_clip_ids(video):
        '''Given a VideoMetadata, get the demographic_clip_ids'''

        result = []
        for video_result in video.job_results: 
            cur_entry = { 
                'gender': video_result.gender, 
                'age': video_result.age, 
                'clip_ids': (video_result.clip_ids + 
                             video.non_job_clip_ids)
            }
            result.append(cur_entry)  
        return result


'''*********************************************************************
VideoHandler
*********************************************************************'''
class VideoHandler(ShareableContentHandler):
    @tornado.gen.coroutine
    def post(self, account_id):
        """handles a Video endpoint post request"""
        schema = Schema({
            Required('account_id'): All(Coerce(str), Length(min=1, max=256)),
            Required('external_video_ref'): All(Any(Coerce(str), unicode),
                Length(min=1, max=512)),
            'url': All(Any(Coerce(str), unicode), Length(min=1, max=2048)),
            'reprocess': Boolean(),
            'integration_id': All(Coerce(str), Length(min=1, max=256)),
            'callback_url': All(Any(Coerce(str), unicode),
                Length(min=1, max=2048)),
            'title': All(Any(Coerce(str), unicode),
                Length(min=1, max=2048)),
            'duration': Any(All(Coerce(float), Range(min=0.0, max=86400.0)),
                None),
            'publish_date': All(CustomVoluptuousTypes.Date()),
            'custom_data': All(CustomVoluptuousTypes.Dictionary()),
            'default_thumbnail_url': All(Any(Coerce(str), unicode),
                Length(min=1, max=2048)),
            'thumbnail_ref': All(Coerce(str), Length(min=1, max=512)),
            'callback_email': All(Coerce(str), Length(min=1, max=2048)),
            'n_thumbs': All(Coerce(int), Range(min=1, max=32)),
            'gender': In(model.predictor.VALID_GENDER),
            'age': In(model.predictor.VALID_AGE_GROUP),
            'n_clips': All(Coerce(int), Range(min=1, max=8)),
            'clip_length': All(Coerce(float), Range(min=0.0)),
            'result_type': In(neondata.ResultType.ARRAY_OF_TYPES),
            'default_clip_url': All(Any(Coerce(str), unicode),
                Length(min=1, max=2048))
        })

        args = self.parse_args()
        args['account_id'] = account_id_api_key = str(account_id)
        result_type = args.get('result_type')
        if not result_type:
            args['result_type'] = neondata.ResultType.THUMBNAILS

        schema(args)

        # Make sure that the external_video_ref is of a form we can handle
        id_match = re.match(neondata.InternalVideoID.VALID_EXTERNAL_REGEX,
                            args['external_video_ref'])
        if (id_match is None or
            id_match.end() != len(args['external_video_ref'])):
            raise Invalid('Invalid video reference. It must work with the '
                          'following regex for all characters: %s' %
                          neondata.InternalVideoID.VALID_EXTERNAL_REGEX)

        reprocess = args.get('reprocess', None)
        url = args.get('url', None)
        if (reprocess is None) == (url is None):
            raise Invalid('Exactly one of reprocess or url is required')
        if reprocess:
            # Do not count a reprocessing towards the limit on the
            # number of videos to process or stop a reprocessing if
            # we're at the limit.
            self.adjust_limits = False
        else:
            try:
                yield self.check_account_limits(
                    self.get_limits_after_prepare()[HTTPVerbs.POST])
            except KeyError:
                pass

        # add the video / request
        video_and_request = yield tornado.gen.Task(
            VideoHelper.create_video_and_request,
            args,
            account_id_api_key)
        new_video = video_and_request[0]
        api_request = video_and_request[1]
        # modify the video if there is a thumbnail set serving_enabled
        def _set_serving_enabled(v):
            v.serving_enabled = len(v.thumbnail_ids) > 0
        yield tornado.gen.Task(neondata.VideoMetadata.modify,
                               new_video.key,
                               _set_serving_enabled)

        # add the job
        sqs_queue = video_processor.video_processing_queue.VideoProcessingQueue()

        account = yield tornado.gen.Task(neondata.NeonUserAccount.get,
                                         account_id)
        duration = new_video.duration

        message = yield sqs_queue.write_message(
                    account.get_processing_priority(),
                    json.dumps(api_request.__dict__),
                    duration)

        if message:
            job_info = {}
            job_info['job_id'] = api_request.job_id
            job_info['video'] = yield self.db2api(new_video,
                                                  api_request)
            statemon.state.increment('post_video_oks')
            self.success(job_info,
                         code=ResponseCode.HTTP_ACCEPTED)
        else:
            raise SubmissionError('Unable to submit job to queue')

    @tornado.gen.coroutine
    def get(self, account_id):
        """handles a Video endpoint get request"""

        schema = Schema({
            Required('account_id'): Any(str, unicode, Length(min=1, max=256)),
            Required('video_id'): Any(
                CustomVoluptuousTypes.CommaSeparatedList()),
            'fields': Any(CustomVoluptuousTypes.CommaSeparatedList())
        })
        args = self.parse_args()
        args['account_id'] = account_id_api_key = str(account_id)
        args = schema(args)
        fields = args.get('fields', None)

        vid_dict = {}
        internal_video_ids = []
        video_ids = args['video_id']

        self._allow_request_by_share_or_raise(
            video_ids,
            neondata.VideoMetadata.__name__)

        for v_id in video_ids:
            internal_video_id = neondata.InternalVideoID.generate(
                account_id_api_key,v_id)
            internal_video_ids.append(internal_video_id)

        videos = yield tornado.gen.Task(neondata.VideoMetadata.get_many,
                                        internal_video_ids)

        vid_dict = yield VideoHelper.build_response(
                       videos,
                       fields,
                       video_ids)

        if vid_dict['video_count'] is 0:
            raise NotFoundError('video(s) do not exist with id(s): %s' %
                                (args['video_id']))

        statemon.state.increment('get_video_oks')
        self.success(vid_dict)

    @tornado.gen.coroutine
    def put(self, account_id):
        """handles a Video endpoint put request"""

        schema = Schema({
            Required('account_id'): Any(str, unicode, Length(min=1, max=256)),
            Required('video_id'): Any(str, unicode, Length(min=1, max=256)),
            'testing_enabled': Coerce(Boolean()),
            'title': Any(str, unicode, Length(min=1, max=1024)),
            'callback_email': CustomVoluptuousTypes.Email(),
            'default_thumbnail_url': All(Any(Coerce(str), unicode),
                Length(min=1, max=2048)),
            'hidden': Boolean()
        })
        args = self.parse_args()
        args['account_id'] = account_id_api_key = str(account_id)
        schema(args)

        if len(self.request.files) > 1:
            raise BadRequestError('Too many files uploaded. Only 1 is allowed')

        internal_video_id = neondata.InternalVideoID.generate(
            account_id_api_key,
            args['video_id'])

        def _update_video(v):
            v.testing_enabled =  Boolean()(
                args.get('testing_enabled', v.testing_enabled))
            v.hidden =  Boolean()(args.get('hidden', v.hidden))


        video = yield neondata.VideoMetadata.modify(
            internal_video_id,
            _update_video,
            async=True)

        # Now add new thumbnails to the video if they are there
        dturl = args.get('default_thumbnail_url', None)
        if dturl or len(self.request.files) == 1: 
            min_rank = yield self._get_min_rank(internal_video_id)
            new_thumb = neondata.ThumbnailMetadata(
                    None,
                    ttype=neondata.ThumbnailType.DEFAULT,
                    rank=min_rank - 1)
        if dturl: 
            yield video.download_and_add_thumbnail(
                new_thumb, 
                image_url=dturl,
                save_objects=True,
                async=True) 
        elif len(self.request.files) == 1: 
            upload = self.request.files.values()[0][0]
            image = PIL.Image.open(io.BytesIO(upload.body))
            yield video.download_and_add_thumbnail(
                new_thumb, 
                image=image,
                save_objects=True,
                async=True) 

        if not video:
            raise NotFoundError('video does not exist with id: %s' %
                (args['video_id']))

        def _modify_tag(t): 
            t.hidden = Boolean()(args.get('hidden', t.hidden))
        if video.tag_id: 
            yield neondata.Tag.modify(
                video.tag_id, 
                _modify_tag, 
                async=True) 

        # we may need to update the request object as well
        db2api_fields = {'testing_enabled', 'video_id'}
        api_request = None
        if video.job_id is not None:
            def _update_request(r):
                r.video_title = args.get('title', r.video_title)
                r.callback_email = args.get('callback_email', r.callback_email)

            api_request = yield neondata.NeonApiRequest.modify(
                video.job_id,
                account_id,
                _update_request,
                async=True)

            db2api_fields.add('title')

        statemon.state.increment('put_video_oks')
        output = yield self.db2api(
            video, api_request,
            fields=list(db2api_fields))
        self.success(output)

    @tornado.gen.coroutine
    def _get_min_rank(self, internal_video_id): 
        min_rank = False 
        video = yield neondata.VideoMetadata.get(
            internal_video_id, 
            async=True) 
        thumbs = yield neondata.ThumbnailMetadata.get_many(
            video.thumbnail_ids, 
            async=True)
        for t in thumbs:
            if t.rank < min_rank: 
                min_rank = t.rank 
        raise tornado.gen.Return(min_rank)
 
    @classmethod
    def get_access_levels(self):
        return {
                 HTTPVerbs.GET : neondata.AccessLevels.READ,
                 HTTPVerbs.POST : neondata.AccessLevels.CREATE,
                 HTTPVerbs.PUT : neondata.AccessLevels.UPDATE,
                 'account_required'  : [HTTPVerbs.GET,
                                        HTTPVerbs.PUT,
                                        HTTPVerbs.POST],
                 'subscription_required' : [HTTPVerbs.POST]
               }

    @classmethod
    def get_limits_after_prepare(self):
        # get_limits() causes the limits to be checked in prepare(),
        # but the limits need to be checked after argument parsing
        # because a video being reprocessed shouldn't count towards
        # the limit.
        post_list = [{ 'left_arg': 'video_posts',
                       'right_arg': 'max_video_posts',
                       'operator': '<',
                       'timer_info': {
                           'refresh_time': 'refresh_time_video_posts',
                           'add_to_refresh_time': 'seconds_to_refresh_video_posts',
                           'timer_resets': [ ('video_posts', 0) ]
                       },
                       'values_to_increase': [ ('video_posts', 1) ],
                       'values_to_decrease': []
        }]
        return {
                   HTTPVerbs.POST: post_list
               }

    @staticmethod
    @tornado.gen.coroutine
    def db2api(video, request, fields=None):
        video_obj = yield VideoHelper.db2api(video, request, fields)
        raise tornado.gen.Return(video_obj)


'''*********************************************************************
VideoStatsHandler
*********************************************************************'''
class VideoStatsHandler(APIV2Handler):
    @tornado.gen.coroutine
    def get(self, account_id):
        """gets the video statuses of 1 -> n videos"""

        schema = Schema({
          Required('account_id'): All(Coerce(str), Length(min=1, max=256)),
          Required('video_id'): Any(CustomVoluptuousTypes.CommaSeparatedList()),
          'fields': Any(CustomVoluptuousTypes.CommaSeparatedList())
        })
        args = self.parse_args()
        args['account_id'] = account_id_api_key = str(account_id)
        args = schema(args)
        internal_video_ids = []
        stats_dict = {}
        video_ids = args['video_id']

        for v_id in video_ids:
            internal_video_id = neondata.InternalVideoID.generate(account_id_api_key,v_id)
            internal_video_ids.append(internal_video_id)

        # even if the video_id does not exist an object is returned
        video_statuses = yield neondata.VideoStatus.get_many(
            internal_video_ids,
            async=True)

        fields = args.get('fields', None)
        video_statuses = yield [self.db2api(x, fields) for x in video_statuses]
        stats_dict['statistics'] = video_statuses
        stats_dict['count'] = len(video_statuses)

        self.success(stats_dict)

    @classmethod
    def get_access_levels(self):
        return {
                 HTTPVerbs.GET: neondata.AccessLevels.READ,
                 'account_required': [HTTPVerbs.GET]
               }

    @classmethod
    def _get_default_returned_fields(cls):
        return ['video_id', 'experiment_state', 'winner_thumbnail']

    @classmethod
    def _get_passthrough_fields(cls):
        return ['experiment_state', 'created', 'updated']

    @classmethod
    @tornado.gen.coroutine
    def _convert_special_field(cls, obj, field):
        if field == 'video_id':
            retval = neondata.InternalVideoID.to_external(
                obj.get_id())
        elif field == 'winner_thumbnail':
            retval = obj.winner_tid
        else:
            raise BadRequestError('invalid field %s' % field)

        raise tornado.gen.Return(retval)


'''*********************************************************************
ThumbnailStatsHandler
*********************************************************************'''
class ThumbnailStatsHandler(APIV2Handler):
    @tornado.gen.coroutine
    def get(self, account_id):
        """handles a thumbnail stats request
           account_id/thumbnail_ids - returns stats information about thumbnails
           account_id/video_id - returns stats information about all thumbnails
                                 for that video
        """

        schema = Schema({
            Required('account_id'): All(Coerce(str), Length(min=1, max=256)),
            'thumbnail_id': Any(CustomVoluptuousTypes.CommaSeparatedList()),
            'video_id': Any(CustomVoluptuousTypes.CommaSeparatedList(20)),
            'fields': Any(CustomVoluptuousTypes.CommaSeparatedList())
        })
        args = self.parse_args()
        args['account_id'] = account_id_api_key = str(account_id)
        args = schema(args)
        thumbnail_ids = args.get('thumbnail_id', None)
        video_ids = args.get('video_id', None)
        if not video_ids and not thumbnail_ids:
            raise Invalid('thumbnail_id or video_id is required')
        if video_ids and thumbnail_ids:
            raise Invalid('you can only have one of thumbnail_id or video_id')

        fields = args.get('fields', None)

        if thumbnail_ids:
            objects = yield tornado.gen.Task(neondata.ThumbnailStatus.get_many,
                                             thumbnail_ids)
        elif video_ids:
            internal_video_ids = []
            # first get all the internal_video_ids
            internal_video_ids = [neondata.InternalVideoID.generate(
                account_id_api_key, x) for x in video_ids]

            # now get all the videos
            videos = yield tornado.gen.Task(neondata.VideoMetadata.get_many,
                                            internal_video_ids)
            # get the list of thumbnail_ids
            thumbnail_ids = []
            for video in videos:
                if video:
                    thumbnail_ids = thumbnail_ids + video.thumbnail_ids
            # finally get the thumbnail_statuses for these things
            objects = yield tornado.gen.Task(neondata.ThumbnailStatus.get_many,
                                             thumbnail_ids)

        # build up the stats_dict and send it back
        stats_dict = {}
        objects = yield [self.db2api(obj, fields)
                         for obj in objects]
        stats_dict['statistics'] = objects
        stats_dict['count'] = len(objects)

        self.success(stats_dict)

    @classmethod
    def get_access_levels(self):
        return {
                 HTTPVerbs.GET: neondata.AccessLevels.READ,
                 'account_required': [HTTPVerbs.GET]
               }

    @classmethod
    def _get_default_returned_fields(cls):
        return ['thumbnail_id', 'video_id', 'ctr']

    @classmethod
    def _get_passthrough_fields(cls):
        return ['serving_frac', 'ctr',
                'created', 'updated']

    @classmethod
    @tornado.gen.coroutine
    def _convert_special_field(cls, obj, field):
        if field == 'video_id':
            retval = neondata.InternalVideoID.from_thumbnail_id(
                obj.get_id())
        elif field == 'thumbnail_id':
            retval = obj.get_id()
        elif field == 'serving_frac':
            retval = obj.serving_frac
        elif field == 'ctr':
            retval = obj.ctr
        elif field == 'impressions':
            retval = obj.imp
        elif field == 'conversions':
            retval = obj.conv
        else:
            raise BadRequestError('invalid field %s' % field)

        raise tornado.gen.Return(retval)


'''
*********************************************************************
LiftStatsHandler
*********************************************************************'''
class LiftStatsHandler(ThumbnailAuth, ShareableContentHandler):

    @tornado.gen.coroutine
    def get(self, account_id):
        schema = Schema({
            Required('account_id'): All(Coerce(str), Length(min=1, max=256)),
            Required('base_id'): All(Coerce(str), Length(min=1, max=2048)),
            Required('thumbnail_ids'): Any(CustomVoluptuousTypes.CommaSeparatedList()),
            'gender': In(model.predictor.VALID_GENDER),
            'age': In(model.predictor.VALID_AGE_GROUP)})
        args = self.parse_args()
        args['account_id'] = account_id_api_key = str(account_id)
        args = schema(args)

        # Check that all the thumbs are keyed to the account.
        query_tids = args['thumbnail_ids']
        self._authorize_thumb_ids_or_raise([args['base_id']] + query_tids)

        base_thumb = yield neondata.ThumbnailMetadata.get(
            args['base_id'],
            async=True)

        # Check that the base thumbnail exists.
        if not base_thumb:
            raise NotFoundError('Base thumbnail does not exist')

        _thumbs = yield neondata.ThumbnailMetadata.get_many(
            query_tids,
            async=True,
            as_dict=True)
        thumbs = {k: t for (k, t) in _thumbs.items() if t}

        self._authorize_thumbs_or_raise([base_thumb] + thumbs.values())

        lift = [
            {'thumbnail_id': k,
            'lift': t.get_estimated_lift(
                base_thumb,
                gender=args.get('gender', None),
                age=args.get('age', None))
                if t else None}
            for k, t in thumbs.items()]

        # Check thumbnail exists.
        rv = {
            'baseline_thumbnail_id': args['base_id'],
            'lift': lift}
        self.success(rv)

    def get_access_levels(self):
        return {HTTPVerbs.GET: neondata.AccessLevels.READ}


'''*********************************************************************
HealthCheckHandler
*********************************************************************'''
class HealthCheckHandler(APIV2Handler):
    @tornado.gen.coroutine
    def get(self):
        apiv1_url = 'http://localhost:%s/healthcheck' % (options.cmsapiv1_port)
        request = tornado.httpclient.HTTPRequest(url=apiv1_url,
                                                 method="GET",
                                                 request_timeout=4.0)
        response = yield tornado.gen.Task(utils.http.send_request, request)
        if response.code is 200:
            self.success('<html>Server OK</html>')
        else:
            raise NotFoundError('unable to get to the v1 api')

    @classmethod
    def get_access_levels(self):
        return {
                 HTTPVerbs.GET: neondata.AccessLevels.NONE
               }

'''*********************************************************************
AccountLimitsHandler : class responsible for returning limit information
                          about an account
   HTTP Verbs     : get
*********************************************************************'''
class AccountLimitsHandler(APIV2Handler):
    @tornado.gen.coroutine
    def get(self, account_id):
        schema = Schema({
          Required('account_id'): All(Coerce(str), Length(min=1, max=256))
        })
        args = self.parse_args()
        args['account_id'] = account_id_api_key = str(account_id)
        schema(args)

        acct_limits = yield neondata.AccountLimits.get(
                          account_id_api_key,
                          async=True)

        if not acct_limits:
            raise NotFoundError()

        result = yield self.db2api(acct_limits)

        self.success(result)

    @classmethod
    def get_access_levels(self):
        return {
                 HTTPVerbs.GET: neondata.AccessLevels.READ,
                 'account_required': [HTTPVerbs.GET]
               }

    @classmethod
    def _get_default_returned_fields(cls):
        return ['video_posts', 'max_video_posts', 'refresh_time_video_posts',
                'max_video_size' ]

    @classmethod
    def _get_passthrough_fields(cls):
        return ['video_posts', 'max_video_posts', 'refresh_time_video_posts',
                'max_video_size' ]

'''*********************************************************************
OptimizelyIntegrationHandler : class responsible for creating/updating/
                               getting an optimizely integration
HTTP Verbs                   : get, post, put
Notes                        : not yet implemented, likely phase 2
*********************************************************************'''
class OptimizelyIntegrationHandler(tornado.web.RequestHandler):
    def __init__(self):
        super(OptimizelyIntegrationHandler, self).__init__()

'''*********************************************************************
LiveStreamHandler : class responsible for creating a new live stream job
   HTTP Verbs     : post
        Notes     : outside of scope of phase 1, future implementation
*********************************************************************'''
class LiveStreamHandler(tornado.web.RequestHandler):
    def __init__(self):
        super(LiveStreamHandler, self).__init__()

'''*********************************************************************
VideoSearchInternalHandler : class responsible for searching videos
                             from an internal source
   HTTP Verbs     : get
*********************************************************************'''
class VideoSearchInternalHandler(APIV2Handler):
    @tornado.gen.coroutine
    def get(self):
        schema = Schema({
            Optional('limit', default=25): All(Coerce(int), Range(min=1, max=100)),
            'account_id': All(Coerce(str), Length(min=1, max=256)),
            'query': str,
            'fields': Any(CustomVoluptuousTypes.CommaSeparatedList()),
            'since': All(Coerce(float)),
            'until': All(Coerce(float))
        })
        args = self.parse_args()
        args = schema(args)

        since = args.get('since')
        until = args.get('until')
        query = args.get('query')

        account_id = args.get('account_id')
        limit = int(args.get('limit'))
        fields = args.get('fields')

        vid_dict = yield VideoHelper.get_search_results(
                       account_id,
                       since,
                       until,
                       query,
                       limit,
                       fields)

        statemon.state.increment(
            ref=_get_internal_search_oks_ref,
            safe=False)

        self.success(vid_dict)

    @classmethod
    def get_access_levels(self):
        return {
                 HTTPVerbs.GET: neondata.AccessLevels.READ,
                 'internal_only': True,
                 'account_required': []
               }

'''*********************************************************************
ShareHandler : class responsible for generating video share tokens
   HTTP Verbs     : get
*********************************************************************'''
class ShareHandler(APIV2Handler):
    @tornado.gen.coroutine
    def get(self, account_id):
        schema = Schema({
            Required('account_id'): All(Coerce(str), Length(min=1, max=256)),
            'video_id': All(Coerce(str), Length(min=1, max=256)),
            'tag_id': All(Coerce(str), Length(min=1, max=256)),
            'clip_id': All(Coerce(str), Length(min=1, max=256))
        })
        args = self.parse_args()
        args['account_id'] = account_id_api_key = str(account_id)
        schema(args)

        resource = yield self._get_resource_or_raise(args)
        token = yield self._get_share_token(resource)
        self.success({'share_token': token})

    @staticmethod
    @tornado.gen.coroutine
    def _get_share_token(resource):
        '''Get token after saving one if missing.'''

        try:
            if resource.share_token:
                raise tornado.gen.Return(resource.share_token)
        except AttributeError:
            pass

        resource.share_token = ShareJWTHelper.encode({
            'content_type': type(resource).__name__,
            'content_id': resource.get_id()
        })
        yield resource.save(async=True)

        raise tornado.gen.Return(resource.share_token)

    @staticmethod
    @tornado.gen.coroutine
    def _get_resource_or_raise(args):
        '''If one of tag_id, video_id, clip_id in args, get it; else raise.'''

        _id = None
        e = BadRequestError('Need exactly one of video_id, tag_id, clip_id')

        if 'video_id' in args:
            _id = neondata.InternalVideoID.generate(
                args['account_id'],
                args['video_id'])
            _class = neondata.VideoMetadata

        if 'tag_id' in args:
            if _id:
                # Can't have more than one id in args.
                raise e
            _id = args['tag_id']
            _class = neondata.Tag

        if 'clip_id' in args:
            if _id:
                raise e
            _id = args['clip_id']
            _class = neondata.Clip

        if not _id:
            raise e

        resource = yield _class.get(_id, async=True)
        if resource:
            try:
                account_id = resource.get_account_id()
            except AttributeError:
                account_id = resource.account_id
            if account_id != args['account_id']:
                raise ForbiddenError()
        if not resource:
            raise NotFoundError('Resource not found for id')

        raise tornado.gen.Return(resource)


    @classmethod
    def get_access_levels(self):
        return {
            HTTPVerbs.GET: neondata.AccessLevels.READ,
            'account_required': [HTTPVerbs.GET]}


'''*********************************************************************
VideoSearchExternalHandler : class responsible for searching videos from
                             an external source
   HTTP Verbs     : get
*********************************************************************'''
class VideoSearchExternalHandler(APIV2Handler):
    @tornado.gen.coroutine
    def get(self, account_id):
        schema = Schema({
            Required('account_id'): All(Coerce(str), Length(min=1, max=256)),
            Optional('limit', default=25): All(Coerce(int), Range(min=1, max=100)),
            'query': Any(CustomVoluptuousTypes.Regex(), str),
            'fields': Any(CustomVoluptuousTypes.CommaSeparatedList()),
            'since': All(Coerce(float)),
            'until': All(Coerce(float)),
            'show_hidden': All(Coerce(bool))

        })
        args = self.parse_args()
        args['account_id'] = str(account_id)
        args = schema(args)
        since = args.get('since')
        until = args.get('until')
        query = args.get('query')
        show_hidden = args.get('show_hidden')

        limit = int(args.get('limit'))
        fields = args.get('fields')

        base_url = '/api/v2/%s/videos/search' % account_id
        vid_dict = yield VideoHelper.get_search_results(
            account_id,
            since,
            until,
            query,
            limit,
            fields,
            base_url=base_url,
            show_hidden=show_hidden)

        statemon.state.increment(
            ref=_get_external_search_oks_ref,
            safe=False)

        self.success(vid_dict)

    @classmethod
    def get_access_levels(self):
        return {
            HTTPVerbs.GET: neondata.AccessLevels.READ,
            'account_required': [HTTPVerbs.GET]}


'''*****************************************************************
AccountIntegrationHandler : class responsible for getting all
                            integrations, on a specific account
  HTTP Verbs      : get
*****************************************************************'''
class AccountIntegrationHandler(APIV2Handler):
    """This is a bit of a one-off API, it will return
          all integrations (regardless of type) for an
          individual account.
    """
    @tornado.gen.coroutine
    def get(self, account_id):
        schema = Schema({
          Required('account_id'): All(Coerce(str), Length(min=1, max=256)),
        })
        args = self.parse_args()
        args['account_id'] = account_id = str(account_id)
        schema(args)

        user_account = yield neondata.NeonUserAccount.get(
            account_id,
            async=True)

        if not user_account:
            raise NotFoundError()

        rv = yield IntegrationHelper.get_integrations(account_id)
        rv['integration_count'] = len(rv['integrations'])
        self.success(rv)

    @classmethod
    def get_access_levels(self):
        return {
                 HTTPVerbs.GET: neondata.AccessLevels.READ,
                 'account_required': [HTTPVerbs.GET]
               }


'''*****************************************************************
UserHandler
*****************************************************************'''
class UserHandler(APIV2Handler):
    """Handles get,put requests to the user endpoint.
       Gets and updates existing users
    """
    @tornado.gen.coroutine
    def get(self, account_id):
        schema = Schema({
          Required('account_id'): All(Coerce(str), Length(min=1, max=256)),
          Required('username'): All(Coerce(str), Length(min=8, max=64)),
        })
        args = self.parse_args()
        args['account_id'] = str(account_id)
        schema(args)

        username = args.get('username')

        user = yield neondata.User.get(
                   username,
                   async=True)

        if not user:
            raise NotFoundError()

        if self.user.username != username:
            raise NotAuthorizedError('Cannot view another users account')

        result = yield self.db2api(user)

        self.success(result)

    @tornado.gen.coroutine
    def put(self, account_id):
        # TODO give ability to modify access_level
        schema = Schema({
          Required('account_id') : All(Coerce(str), Length(min=1, max=256)),
          Required('username') : All(Coerce(str), Length(min=8, max=64)),
          'first_name': All(Coerce(str), Length(min=1, max=256)),
          'last_name': All(Coerce(str), Length(min=1, max=256)),
          'secondary_email': All(Coerce(str), Length(min=1, max=256)),
          'cell_phone_number': All(Coerce(str), Length(min=1, max=32)),
          'title': All(Coerce(str), Length(min=1, max=32)),
          'send_emails': Boolean()
        })
        args = self.parse_args()
        args['account_id'] = str(account_id)
        schema(args)
        username = args.get('username')

        if self.user.access_level is not neondata.AccessLevels.GLOBAL_ADMIN:
            if self.user.username != username:
                raise NotAuthorizedError('Cannot update another\
                               users account')

        def _update_user(u):
            u.first_name = args.get('first_name', u.first_name)
            u.last_name = args.get('last_name', u.last_name)
            u.title = args.get('title', u.title)
            u.cell_phone_number = args.get(
                'cell_phone_number',
                u.cell_phone_number)
            u.secondary_email = args.get(
                'secondary_email',
                u.secondary_email)
            u.send_emails = Boolean()(args.get(
                'send_emails', 
                u.send_emails))

        user_internal = yield neondata.User.modify(
            username,
            _update_user,
            async=True)

        if not user_internal:
            raise NotFoundError()

        result = yield self.db2api(user_internal)

        self.success(result)

    @classmethod
    def get_access_levels(cls):
        return {
                 HTTPVerbs.GET: neondata.AccessLevels.READ,
                 HTTPVerbs.PUT: neondata.AccessLevels.UPDATE,
                 'account_required' : [HTTPVerbs.GET, HTTPVerbs.PUT]
               }

    @classmethod
    def _get_default_returned_fields(cls):
        return ['username', 'created', 'updated',
                'first_name', 'last_name', 'title',
                'secondary_email', 'cell_phone_number',
                'access_level' ]

    @classmethod
    def _get_passthrough_fields(cls):
        return ['username', 'created', 'updated',
                'first_name', 'last_name', 'title',
                'secondary_email', 'cell_phone_number',
                'access_level' ]

'''*****************************************************************
BillingAccountHandler
*****************************************************************'''
class BillingAccountHandler(APIV2Handler):
    """This talks to a sevice and creates a billing account with our
          external billing integration (currently stripe).

       This acts as an upreate function, essentially always call
        post, to save account information on the recurly side of
        things.
    """
    @tornado.gen.coroutine
    def post(self, account_id):
        schema = Schema({
          Required('account_id') : All(Coerce(str), Length(min=1, max=256)),
          Required('billing_token_ref') : All(
              Coerce(str),
              Length(min=1, max=512))
        })
        args = self.parse_args()
        args['account_id'] = str(account_id)
        schema(args)
        billing_token_ref = args.get('billing_token_ref')
        account = yield neondata.NeonUserAccount.get(
            account_id,
            async=True)

        if not account:
            raise NotFoundError('Neon Account required.')

        customer_id = None

        @tornado.gen.coroutine
        def _create_account():
            customer = yield self.executor.submit(
                stripe.Customer.create,
                email=account.email,
                source=billing_token_ref)
            cid = customer.id
            _log.info('New Stripe customer %s created with id %s' % (
                account.email, cid))
            raise tornado.gen.Return(customer)

        try:
            if account.billing_provider_ref:
                customer = yield self.executor.submit(
                    stripe.Customer.retrieve,
                    account.billing_provider_ref)

                customer.email = account.email or customer.email
                customer.source = billing_token_ref
                customer_id = customer.id
                yield self.executor.submit(customer.save)
            else:
                customer = yield _create_account()
        except stripe.error.InvalidRequestError as e:
            if 'No such customer' in str(e):
                # this is here just in case the ref got
                # screwed up, it should rarely if ever happen
                customer = yield _create_account()
            else:
                _log.error('Invalid request error we do not handle %s' % e)
                raise
        except Exception as e:
            _log.error('Unknown error occurred talking to Stripe %s' % e)
            raise

        def _modify_account(a):
            a.billed_elsewhere = False
            a.billing_provider_ref = customer.id

        yield neondata.NeonUserAccount.modify(
            account.neon_api_key,
            _modify_account,
            async=True)

        result = yield self.db2api(customer)

        self.success(result)

    @tornado.gen.coroutine
    def get(self, account_id):
        schema = Schema({
          Required('account_id') : Any(str, unicode, Length(min=1, max=256))
        })
        args = self.parse_args()
        args['account_id'] = str(account_id)
        schema(args)

        account = yield neondata.NeonUserAccount.get(
            account_id,
            async=True)

        if not account:
            raise NotFoundError('Neon Account required.')

        if not account.billing_provider_ref:
            raise NotFoundError('No billing account found - no ref.')

        try:
            customer = yield self.executor.submit(
                stripe.Customer.retrieve,
                account.billing_provider_ref)

        except stripe.error.InvalidRequestError as e:
            if 'No such customer' in str(e):
                raise NotFoundError('No billing account found - not in stripe')
            else:
                _log.error('Unknown invalid error occurred talking'\
                           ' to Stripe %s' % e)
                raise Exception('Unknown Stripe Error')
        except Exception as e:
            _log.error('Unknown error occurred talking to Stripe %s' % e)
            raise

        result = yield self.db2api(customer)
        self.success(result)

    @classmethod
    def _get_default_returned_fields(cls):
        return ['id', 'account_balance', 'created', 'currency',
                'default_source', 'delinquent', 'description',
                'discount', 'email', 'livemode', 'subscriptions',
                'metadata', 'sources']

    @classmethod
    def _get_passthrough_fields(cls):
        return ['id', 'account_balance', 'created', 'currency',
                'default_source', 'delinquent', 'description',
                'discount', 'email', 'livemode']

    @classmethod
    @tornado.gen.coroutine
    def _convert_special_field(cls, obj, field):
        if field == 'subscriptions':
            retval = obj.subscriptions.to_dict()
        elif field == 'sources':
            retval = obj.sources.to_dict()
        elif field == 'metadata':
            retval = obj.metadata.to_dict()
        else:
            raise BadRequestError('invalid field %s' % field)

        raise tornado.gen.Return(retval)


    @classmethod
    def get_access_levels(cls):
        return {
                 HTTPVerbs.POST : neondata.AccessLevels.CREATE,
                 HTTPVerbs.GET : neondata.AccessLevels.READ,
                 'account_required'  : [HTTPVerbs.POST, HTTPVerbs.GET]
               }

'''*****************************************************************
BillingSubscriptionHandler
*****************************************************************'''
class BillingSubscriptionHandler(APIV2Handler):
    """This talks to recurly and creates a billing subscription with our
          recurly integration.
    """
    @tornado.gen.coroutine
    def post(self, account_id):
        schema = Schema({
          Required('account_id') : All(Coerce(str), Length(min=1, max=256)),
          Required('plan_type'): All(Coerce(str), Length(min=1, max=32))
        })
        args = self.parse_args()
        args['account_id'] = account_id = str(account_id)
        schema(args)
        plan_type = args.get('plan_type')

        account = yield neondata.NeonUserAccount.get(
            account_id,
            async=True)

        billing_plan = yield neondata.BillingPlans.get(
            plan_type,
            async=True)

        if not billing_plan:
            raise NotFoundError('No billing plan for that plan_type')

        if not account:
            raise NotFoundError('Neon Account was not found')

        if not account.billing_provider_ref:
            raise NotFoundError(
                'There is not a billing account set up for this account')
        try:
            original_plan_type = account.subscription_information['plan']['id']
        except TypeError:
            original_plan_type = None

        try:
            customer = yield self.executor.submit(
                stripe.Customer.retrieve,
                account.billing_provider_ref)

            # get all subscriptions, they are sorted
            # by most recent, if there are not any, just
            # submit the new one, otherwise cancel the most
            # recent and submit the new one
            cust_subs = yield self.executor.submit(
                customer.subscriptions.all)

            if len(cust_subs['data']) > 0:
                cancel_me = cust_subs['data'][0]
                yield self.executor.submit(cancel_me.delete)

            if plan_type == 'demo':
                subscription = stripe.Subscription(id='canceled')
                def _modify_account(a):
                    a.subscription_information = None
                    a.billed_elsewhere = True
                    a.billing_provider_ref = None
                    a.verify_subscription_expiry = None
                # cancel all the things!
                cards = yield self.executor.submit(
                    customer.sources.all,
                    object='card')
                for card in cards:
                    yield self.executor.submit(card.delete)
                _log.info('Subscription downgraded for account %s' %
                     account.neon_api_key)
            else:
                subscription = yield self.executor.submit(
                    customer.subscriptions.create,
                    plan=plan_type)
                def _modify_account(a):
                    a.serving_enabled = True
                    a.subscription_information = subscription
                    a.verify_subscription_expiry = \
                        (datetime.utcnow() + timedelta(
                        seconds=options.get(
                        'cmsapiv2.apiv2.check_subscription_interval'))
                        ).strftime(
                            "%Y-%m-%d %H:%M:%S.%f")

                _log.info('New subscription created for account %s' %
                    account.neon_api_key)

            yield neondata.NeonUserAccount.modify(
                account.neon_api_key,
                _modify_account,
                async=True)

        except stripe.error.InvalidRequestError as e:
            if 'No such customer' in str(e):
                _log.error('Billing mismatch for account %s' % account.email)
                raise NotFoundError('No billing account found in Stripe')

            _log.error('Unhandled InvalidRequestError\
                 occurred talking to Stripe %s' % e)
            raise
        except stripe.error.CardError as e:
            raise
        except Exception as e:
            _log.error('Unknown error occurred talking to Stripe %s' % e)
            raise

        billing_plan = yield neondata.BillingPlans.get(
            plan_type.lower(),
            async=True)

        # only update limits if we have actually changed the plan type
        if original_plan_type != plan_type.lower():
            def _modify_limits(a):
                a.populate_with_billing_plan(billing_plan)

            yield neondata.AccountLimits.modify(
                account.neon_api_key,
                _modify_limits,
                create_missing=True,
                async=True)

        result = yield self.db2api(subscription)

        self.success(result)

    @tornado.gen.coroutine
    def get(self, account_id):
        schema = Schema({
          Required('account_id') : Any(str, unicode, Length(min=1, max=256))
        })
        args = self.parse_args()
        args['account_id'] = str(account_id)
        schema(args)

        account = yield neondata.NeonUserAccount.get(
            account_id,
            async=True)

        if not account:
            raise NotFoundError('Neon Account required.')

        if not account.billing_provider_ref:
            raise NotFoundError('No billing account found - no ref.')

        try:
            customer = yield self.executor.submit(
                stripe.Customer.retrieve,
                account.billing_provider_ref)

            cust_subs = yield self.executor.submit(
                customer.subscriptions.all)

            most_recent_sub = cust_subs['data'][0]
        except stripe.error.InvalidRequestError as e:
            if 'No such customer' in str(e):
                raise NotFoundError('No billing account found - not in stripe')
            else:
                _log.error('Unknown invalid error occurred talking'\
                           ' to Stripe %s' % e)
                raise Exception('Unknown Stripe Error')
        except IndexError:
            raise NotFoundError('A subscription was not found.')
        except Exception as e:
            _log.error('Unknown error occurred talking to Stripe %s' % e)
            raise

        result = yield self.db2api(most_recent_sub)

        self.success(result)

    @classmethod
    def _get_default_returned_fields(cls):
        return ['id', 'application_fee_percent', 'cancel_at_period_end',
                'canceled_at', 'current_period_end', 'current_period_start',
                'customer', 'discount', 'ended_at', 'plan',
                'quantity', 'start', 'tax_percent', 'trial_end',
                'trial_start']

    @classmethod
    def _get_passthrough_fields(cls):
        return ['id', 'application_fee_percent', 'cancel_at_period_end',
                'canceled_at', 'current_period_end', 'current_period_start',
                'customer', 'discount', 'ended_at', 'metadata', 'plan',
                'quantity', 'start', 'tax_percent', 'trial_end',
                'trial_start']

    @classmethod
    @tornado.gen.coroutine
    def _convert_special_field(cls, obj, field):
        if field == 'metadata':
            retval = obj.metadata.to_dict()
        else:
            raise BadRequestError('invalid field %s' % field)

        raise tornado.gen.Return(retval)

    @classmethod
    def get_access_levels(cls):
        return {
                 HTTPVerbs.POST : neondata.AccessLevels.CREATE,
                 HTTPVerbs.GET : neondata.AccessLevels.READ,
                 'account_required'  : [HTTPVerbs.POST]
               }

'''*********************************************************************
TelemetrySnippetHandler : class responsible for creating the telemetry snippet
   HTTP Verbs     : get
*********************************************************************'''
class TelemetrySnippetHandler(APIV2Handler):
    @tornado.gen.coroutine
    def get(self, account_id):
        '''Generates a telemetry snippet for a given account'''

        schema = Schema({
            Required('account_id') : All(Coerce(str), Length(min=1, max=256)),
            })
        args = self.parse_args()
        args['account_id'] = account_id_api_key = str(account_id)
        data = schema(args)

        # Find out if there is a Gallery integration
        integrations = yield self.account.get_integrations(async=True)

        using_gallery = any([x.uses_bc_gallery for x in integrations if
                             isinstance(x, neondata.BrightcoveIntegration)])

        # Build the snippet
        if using_gallery:
            template = (
                '<!-- Neon -->',
                '<script id="neon">',
                "  var neonPublisherId = '{tai}';",
                "  var neonBrightcoveGallery = true;",
                '</script>',
                "<script src='//cdn.neon-lab.com/neonoptimizer_dixon.js'></script>',",
                '<!-- Neon -->'
                )
        else:
            template = (
                '<!-- Neon -->',
                '<script id="neon">',
                "  var neonPublisherId = '{tai}';",
                '</script>',
                "<script src='//cdn.neon-lab.com/neonoptimizer_dixon.js'></script>',",
                '<!-- Neon -->'
                )

        self.set_header('Content-Type', 'text/plain')
        self.success('\n'.join(template).format(
            tai=self.account.tracker_account_id))

    @classmethod
    def get_access_levels(cls):
        return {
                 HTTPVerbs.GET : neondata.AccessLevels.READ,
                 'account_required'  : [HTTPVerbs.GET]
               }

'''*****************************************************************
BatchHandler 
*****************************************************************'''
class BatchHandler(APIV2Handler):
    @tornado.gen.coroutine
    def post(self):
        schema = Schema({
          Required('call_info') : All(CustomVoluptuousTypes.Dictionary())
        })
      
        args = self.parse_args()
        schema(args)
         
        call_info = args['call_info']
        access_token = call_info.get('access_token', None) 
        refresh_token = call_info.get('refresh_token', None)
        
        client = cmsapiv2.client.Client(
            access_token=access_token,  
            refresh_token=refresh_token,
            skip_auth=True)

        requests = call_info.get('requests', None)
        output = { 'results' : [] }
        for req in requests: 
            # request will be information about 
            # the call we want to make 
            result = {} 
            try:
                result['relative_url'] = req['relative_url'] 
                result['method'] = req['method']
 
                method = req['method'] 
                http_req = tornado.httpclient.HTTPRequest(
                    req['relative_url'], 
                    method=method) 

                if method == 'POST' or method == 'PUT': 
                    http_req.headers = {"Content-Type" : "application/json"}
                    http_req.body = json.dumps(req.get('body', None))
                
                response = yield client.send_request(http_req)
                if response.error:
                    error = { 'error' : 
                        { 
                            'message' : response.reason, 
                            'code' : response.code 
                        } 
                    }
                    result['response'] = error
                    result['response_code'] = response.code 
                else:  
                    result['relative_url'] = req['relative_url'] 
                    result['method'] = req['method'] 
                    result['response'] = json.loads(response.body)
                    result['response_code'] = response.code
            except AttributeError:
                result['response'] = 'Malformed Request'
                result['response_code'] = ResponseCode.HTTP_BAD_REQUEST 
            except Exception as e: 
                result['response'] = 'Unknown Error Occurred' 
                result['response_code'] = ResponseCode.HTTP_INTERNAL_SERVER_ERROR
            finally: 
                output['results'].append(result)
                 
        self.success(output) 
 
    @classmethod
    def get_access_levels(cls):
        return { 
                 HTTPVerbs.POST : neondata.AccessLevels.NONE 
               }

class EmailHandler(APIV2Handler): 
    @tornado.gen.coroutine
    def post(self, account_id):
        schema = Schema({
            Required('account_id') : All(Coerce(str), Length(min=1, max=256)), 
            Required('template_slug') : All(Coerce(str), Length(min=1, max=512)), 
            'template_args' : All(Coerce(str), Length(min=1, max=2048)),
            'to_email_address' : All(Coerce(str), Length(min=1, max=1024)),
            'from_email_address' : All(Coerce(str), Length(min=1, max=1024)),
            'from_name' : All(Coerce(str), Length(min=1, max=1024)),
            'subject' : All(Coerce(str), Length(min=1, max=1024)),
            'reply_to' : All(Coerce(str), Length(min=1, max=1024))
        })

        args = self.parse_args()
        args['account_id'] = account_id_api_key = str(account_id)
        data = schema(args)
        
        args_email = args.get('to_email_address', None)
        template_args = args.get('template_args', None)  
        template_slug = args.get('template_slug') 
          
        cur_user = self.user 
        if cur_user:
            cur_user_email = cur_user.username

        if args_email: 
            send_to_email = args_email
        elif cur_user_email: 
            send_to_email = cur_user_email
            if not cur_user.send_emails: 
                self.success({'message' : 'user does not want emails'})
        else:  
            raise NotFoundError('Email address is required.')
        
        yield MandrillEmailSender.send_mandrill_email(
            send_to_email, 
            template_slug, 
            template_args=template_args)
 
        self.success({'message' : 'Email sent to %s' % send_to_email })
 
    @classmethod
    def get_limits(self):
        post_list = [{ 'left_arg': 'email_posts',
                       'right_arg': 'max_email_posts',
                       'operator': '<',
                       'timer_info': {
                           'refresh_time': 'refresh_time_email_posts',
                           'add_to_refresh_time': 'seconds_to_refresh_email_posts',
                           'timer_resets': [ ('email_posts', 0) ]
                       },
                       'values_to_increase': [ ('email_posts', 1) ],
                       'values_to_decrease': []
        }]
        return {
                   HTTPVerbs.POST: post_list
               }
            
    @classmethod
    def get_access_levels(cls):
        return { 
                 HTTPVerbs.POST : neondata.AccessLevels.CREATE, 
                 'account_required'  : [HTTPVerbs.POST] 
               }

class FeatureHandler(APIV2Handler):
    @tornado.gen.coroutine
    def get(self):
        schema = Schema({
            'key' : Any(CustomVoluptuousTypes.CommaSeparatedList(
                at_least_x=1, 
                min_length_for_elements=1)), 
            'model_name' : All(Coerce(str), Length(min=1, max=512)), 
            'fields': Any(CustomVoluptuousTypes.CommaSeparatedList())
        })
        args = self.parse_args()
        args = schema(args)
        model_name = args.get('model_name', None)
        keys = args.get('key', None)

        if (model_name is None) == (keys is None):
            raise Invalid('Exactly one of model_name or key is required')

        fields = args.get('fields', None)
        
        # if keys is set
        if keys: 
            features = yield neondata.Feature.get_many(keys, async=True)
        else: 
            features = yield neondata.Feature.get_by_model_name(
                model_name, 
                async=True)
     
        res_list = yield [self.db2api(f, fields=fields) for f in features] 
        
        rv = { 'features' : res_list, 
               'feature_count' : len(res_list) }
 
        self.success(rv)

    @classmethod
    def _get_default_returned_fields(cls):
        return ['key', 'model_name', 'created', 'updated', 
                'name', 'variance_explained', 'index'] 

    @classmethod
    def _get_passthrough_fields(cls):
        return ['key', 'model_name', 'created', 'updated', 
                'name', 'variance_explained', 'index'] 

    @classmethod
    def get_access_levels(cls):
        return {
                 HTTPVerbs.GET : neondata.AccessLevels.NONE
               }


class EmailSupportHandler(APIV2Handler):
    '''Allow visitor to send email to Neon without an account.'''

    SUPPORT_ADDRESS = 'support@neon-lab.com'
    # Reference: https://mandrillapp.com/templates/code?id=support-email-admin
    SUPPORT_TEMPLATE_SLUG = 'support-email-admin'

    @tornado.gen.coroutine
    def post(self):
        '''Send the content of "message" as an email to Neon support.'''
        schema = Schema({
            Required('from_email'): All(Coerce(
                CustomVoluptuousTypes.Email()), Length(min=1, max=1024)),
            Required('from_name') : All(Coerce(str), Length(min=1, max=1024)),
            Required('message') : All(Coerce(str), Length(min=1, max=4096))
        })

        args = self.parse_args()
        args = schema(args)

        yield MandrillEmailSender.send_mandrill_email(
            self.SUPPORT_ADDRESS,
            self.SUPPORT_TEMPLATE_SLUG,
            template_args=args,
            reply_to=args['from_email'])

        self.success({'message' : 'Email sent to %s' % self.SUPPORT_ADDRESS})

    @classmethod
    def get_access_levels(cls):
        return {HTTPVerbs.POST: neondata.AccessLevels.NONE}

class SocialImageHandler(ShareableContentHandler):
    '''Endpoint that creates composite images to share on social'''

    # Maps the platform name to 
    # (image_width, image_height, box_height, font_size)
    PLATFORM_MAP = {
        'twitter' : (875, 500, 70, 38),
        'facebook' : (800, 800, 67, 38),
        '' : (800, 800, 67, 38),
        None : (800, 800, 67, 38)
    }

    @tornado.gen.coroutine
    def get(self, account_id, platform):
        '''On 200, returns a JPG image for sharing on social that is 
        composed of the baseline thumb and our best thumb.
        '''
        Schema({
            Required('account_id'): All(Coerce(str), Length(min=1, max=256)),
            'platform': In(['twitter', '', None, 'facebook'])})(self.args)

        try:
            # See if we can get the asset id from the payload
            payload = self.share_payload
            if not payload:
                raise BadRequestError('This endpoint requires a share token', ResponseCode.HTTP_BAD_REQUEST)

            pl_id = payload['content_id']
            pl_type = payload['content_type']

            # Find a thumb to display.
            # The is_authorized check validates these exist and match
            # their share token with the share token.
            if pl_type == neondata.VideoMetadata.__name__:
                video = yield neondata.VideoMetadata.get(pl_id, async=True)
                best_thumb = yield self._get_best_thumb_of_video(video)
            elif pl_type == neondata.Tag.__name__:
                tag = yield neondata.Tag.get(pl_id, async=True)
                best_thumb = yield self._get_best_thumb_of_tag(tag)

                # If the tag is of video and the video has a clip,
                # use the best clip's score in place of the thumbnail's.
                try:
                    if tag.tag_type == neondata.TagType.VIDEO:
                        video = yield neondata.VideoMetadata.get(
                            tag.video_id,
                            async=True)
                        dems = VideoHelper.get_demographic_clip_ids(video)

                        if dems:
                            # Get the best clip of the null gender and age group.
                            clips = (d for d in dems
                                     if d['age'] is None and
                                        d['gender'] is None).next()
                            best_clip_id = clips['clip_ids'][0]

                            clip = yield neondata.Clip.get(
                                best_clip_id,
                                async=True)
                            clip_thumb = yield neondata.ThumbnailMetadata.get(
                                clip.thumbnail_id,
                                async=True)
                            # Override the best thumb with this one, and the
                            # clip's score.
                            if clip_thumb:
                                best_thumb = clip_thumb
                                best_thumb.model_version = None
                                best_thumb.model_score = clip.score
                except Exception as e:
                    _log.warn('Problem using clip thumb %s', e)
                    # Fail back to using the best thumbnail.
                    

            elif pl_type == neondata.Clip.__name__:
                clip = yield neondata.Clip.get(pl_id, async=True)
                best_thumb = yield neondata.ThumbnailMetadata.get(clip.thumbnail_id)
            else:
                raise ForbiddenError('Invalid token')


            # Get the size needs based on the platform
            width, height, box_height, font_size = SocialImageHandler.PLATFORM_MAP[
                platform]
                
            # Now, we build the image.
            image = yield self._build_image(
                best_thumb,
                width,
                height,
                box_height,
                font_size)

            buf = StringIO()
            image.save(buf, 'jpeg', quality=90)

            # Finally, write the image data to JPEG in the output
            self.set_header('Content-Type', 'image/jpg')
            self.set_status(200)
            self.write(buf.getvalue())
            self.finish()

        except Exception as e:
            statemon.state.increment('social_image_invalid_request')
            raise e

        statemon.state.increment('social_image_generated')

    @tornado.gen.coroutine
    def _build_image(self, best_thumb, width, height, box_height, font_size):
        canvas = yield self._get_image_of_size(best_thumb, width, height)

        # Draw the icon on the good thumbnail
        icon = PIL.Image.open(os.path.join(
            os.path.dirname(__file__), 'images', 'NeonScore.png'))
        icon = icon.resize(
            (icon.size[0] * box_height / icon.size[1], box_height),
            PIL.Image.ANTIALIAS)
        canvas.paste(icon, (0, height-box_height), mask=icon.split()[3])

        # Write the scores
        font = PIL.ImageFont.truetype(
            os.path.join(os.path.dirname(__file__), 'fonts', 'balto-book.otf'),
            size=font_size,
            index=0)
        draw = PIL.ImageDraw.Draw(canvas)
        draw.text((int(box_height + 0.24*box_height),
                   int(height-box_height+0.22*box_height)),
                  '%2d' % best_thumb.get_neon_score(),
                  font=font, fill='#FFFFFF')

        raise tornado.gen.Return(canvas)

    @tornado.gen.coroutine
    def _get_image_of_size(self, thumb, width, height):
        '''Returns a PIL image that is WxH of a given thumb.'''
        is_download_error = False
        # First see if there's a serving url we can grab
        urls = yield neondata.ThumbnailServingURLs.get(thumb.key, async=True)
        if urls is not None:
            try:
                url = urls.get_serving_url(width, height)
                im = yield PILImageUtils.download_image(url, async=True)
                raise tornado.gen.Return(im)
            except KeyError:
                pass
            except IOError:
                is_download_error = True

        # We could not get the correct sized image, so cut it
        full_im = None
        for url in thumb.urls:
            is_download_error = False
            try:
                full_im = yield PILImageUtils.download_image(url, async=True)
                break
            except IOError:
                is_download_error = True
            
        if full_im:
            cv_im = utils.pycvutils.resize_and_crop(
                PILImageUtils.to_cv(full_im),
                height,
                width)
            raise tornado.gen.Return(PILImageUtils.from_cv(cv_im))

        if is_download_error:
            msg = 'Error downloading source image for thumb %s' % thumb.key
            _log.error(msg)
            raise ResourceDownloadError(msg)
        else:
            msg = ('Could not generate a rendition for image: %s' % 
                    thumb.key)
            _log.warn(msg)
            raise Invalid(msg)

    @tornado.gen.coroutine
    def _get_best_thumb_of_video(self, video):
        '''Returns the (base, best) ThumbnailMetadata objects.'''

        # Get the job thumbnails
        thumb_group = [x for x in video.job_results 
                       if x.age is None and x.gender is None]
        job_thumb_ids = (thumb_group[0].thumbnail_ids if any(thumb_group) 
                         else None)
        if not job_thumb_ids:
            job_thumb_ids = video.thumbnail_ids

        job_thumbs = yield neondata.ThumbnailMetadata.get_many(job_thumb_ids,
                                                               async=True)

        job_thumbs = [x for x in job_thumbs if x is not None and
                      x.type == neondata.ThumbnailType.NEON]
        
        if len(job_thumbs) == 0:
            raise Invalid('Video does not have any valid good thumbs')

        # Sort the job thumbs ascending by model score
        job_thumbs = sorted(job_thumbs, key=lambda x: x.get_score())

        best_thumb = job_thumbs[-1]

        raise tornado.gen.Return(best_thumb)

    @tornado.gen.coroutine
    def _get_best_thumb_of_tag(self, tag):
        thumb_ids = yield neondata.TagThumbnail.get(tag_id=tag.get_id(), async=True)
        thumbnails = yield neondata.ThumbnailMetadata.get_many(
            thumb_ids,
            async=True)

        # Sort the job thumbs ascending by model score
        thumbnails = sorted(thumbnails, key=lambda x: x.get_score())

        if len(thumbnails) == 0:
            raise Invalid('Tag does not have any associated thumbnail')
        best_thumb = thumbnails[-1]
        raise tornado.gen.Return(best_thumb)

    @classmethod
    def get_access_levels(cls):
        return {HTTPVerbs.GET: neondata.AccessLevels.READ}

class ClipHandler(ShareableContentHandler):
    @tornado.gen.coroutine
    def get(self, account_id):
        schema = Schema({
            Required('account_id'): All(Coerce(str), Length(min=1, max=256)),
            'clip_ids': Any(CustomVoluptuousTypes.CommaSeparatedList()),
            'fields': Any(CustomVoluptuousTypes.CommaSeparatedList())
        })

        args = self.parse_args()
        args['account_id'] = account_id_api_key = str(account_id)
        clip_ids = args['clip_ids'].split(',')
        fields = args.get('fields')
        if fields:
            fields = fields.split(',')

        _clips = yield neondata.Clip.get_many(
            clip_ids,
            create_default=False,
            log_missing=False,
            async=True)
        clips_dict = {}
        clips = yield [self.db2api(obj, fields) for obj in _clips]
        clips_dict['clips'] = clips
        clips_dict['count'] = len(clips)
        self.success(clips_dict)

    @classmethod
    def _get_default_returned_fields(cls):
        return ['video_id', 'clip_id', 'rank', 'start_frame',
                'enabled', 'url', 'end_frame', 'type',
                'created', 'updated', 'neon_score', 'duration',
                'thumbnail_id']

    @classmethod
    def _get_passthrough_fields(cls):
        return ['rank', 'start_frame', 'type', 'duration',
                'enabled', 'end_frame',
                'created', 'updated', 'thumbnail_id']

    @classmethod
    @tornado.gen.coroutine
    def _convert_special_field(cls, obj, field, age=None, gender=None):
        if field == 'video_id':
            retval = neondata.InternalVideoID.to_external(obj.video_id)
        elif field == 'clip_id':
            retval = obj.key
        elif field == 'url':
            retval = obj.urls[0] if obj.urls else None
        elif field == 'renditions':
            renditions = yield neondata.VideoRendition.search_for_objects(
                clip_id=obj.get_id(), async=True)
            retval = [x.__dict__ for x in renditions]
        elif field == 'neon_score':
            retval = obj.get_neon_score()
        else:
            raise BadRequestError('invalid field %s' % field)

        raise tornado.gen.Return(retval)

    @classmethod
    def get_access_levels(self):
        return {HTTPVerbs.GET: neondata.AccessLevels.READ}

class AWSURLHandler(APIV2Handler):
    '''Let user get pre-signed CDN URL for direct upload.'''

    def get(self, account_id):
        schema = Schema({
            Required('account_id'): All(Coerce(str), Length(min=1, max=256)),
            Required('filename'): All(Coerce(str), Length(min=1, max=256)),
        })
        args = schema(self.args)
        bucket = 'neon-user-video-upload'
        key = '%s/%s' % (account_id, args['filename'])
        signed = AWSHosting.get_signed_url(bucket, key)
        self.success({
            'url': signed['url'],
            'expires_at': signed['expires_at']})

    @classmethod
    def get_access_levels(cls):
        return {HTTPVerbs.GET: neondata.AccessLevels.READ}


'''*********************************************************************
Endpoints
*********************************************************************'''
application = tornado.web.Application([
    (r'/api/v2/batch/?$', BatchHandler),
    (r'/api/v2/feature/?$', FeatureHandler),
    (r'/api/v2/tags/search/?$', TagSearchInternalHandler),
    (r'/api/v2/videos/search/?$', VideoSearchInternalHandler),
    (r'/api/v2/(\d+)/live_stream', LiveStreamHandler),
    (r'/api/v2/email/support/?$', EmailSupportHandler),
    (r'/api/v2/([a-zA-Z0-9]+)/?$', AccountHandler),
    (r'/api/v2/([a-zA-Z0-9]+)/billing/account/?$', BillingAccountHandler),
    (r'/api/v2/([a-zA-Z0-9]+)/billing/subscription/?$',
        BillingSubscriptionHandler),
    (r'/api/v2/([a-zA-Z0-9]+)/clips/share/?$', ShareHandler),
    (r'/api/v2/([a-zA-Z0-9]+)/email/?$', EmailHandler),
    (r'/api/v2/([a-zA-Z0-9]+)/integrations/?$',
        AccountIntegrationHandler),
    (r'/api/v2/([a-zA-Z0-9]+)/integrations/brightcove/?$',
        BrightcoveIntegrationHandler),
    (r'/api/v2/([a-zA-Z0-9]+)/integrations/brightcove/players/?$',
        BrightcovePlayerHandler),
    (r'/api/v2/([a-zA-Z0-9]+)/integrations/ooyala/?$',
        OoyalaIntegrationHandler),
    (r'/api/v2/([a-zA-Z0-9]+)/integrations/optimizely/?$',
        OptimizelyIntegrationHandler),
    (r'/api/v2/([a-zA-Z0-9]+)/limits/?$', AccountLimitsHandler),
    (r'/api/v2/([a-zA-Z0-9]+)/social/image/?([a-z]*)/?$', SocialImageHandler),
    (r'/api/v2/([a-zA-Z0-9]+)/statistics/estimated_lift/?$', LiftStatsHandler),
    (r'/api/v2/([a-zA-Z0-9]+)/statistics/thumbnails/?$', ThumbnailStatsHandler),
    (r'/api/v2/([a-zA-Z0-9]+)/statistics/videos/?$', VideoStatsHandler),
    (r'/api/v2/([a-zA-Z0-9]+)/stats/estimated_lift/?$', LiftStatsHandler),
    (r'/api/v2/([a-zA-Z0-9]+)/stats/thumbnails/?$',
        ThumbnailStatsHandler),
    (r'/api/v2/([a-zA-Z0-9]+)/stats/videos/?$', VideoStatsHandler),
    (r'/api/v2/([a-zA-Z0-9]+)/tags/?$', TagHandler),
    (r'/api/v2/([a-zA-Z0-9]+)/tags/search/?$', TagSearchExternalHandler),
    (r'/api/v2/([a-zA-Z0-9]+)/tags/share/?$', ShareHandler),
    (r'/api/v2/([a-zA-Z0-9]+)/telemetry/snippet/?$', TelemetrySnippetHandler),
    (r'/api/v2/([a-zA-Z0-9]+)/thumbnails/?$', ThumbnailHandler),
    (r'/api/v2/([a-zA-Z0-9]+)/clips/?$', ClipHandler),
    (r'/api/v2/([a-zA-Z0-9]+)/users/?$', UserHandler),
    (r'/api/v2/([a-zA-Z0-9]+)/videos/?$', VideoHandler),
    (r'/api/v2/([a-zA-Z0-9]+)/videos/search/?$', VideoSearchExternalHandler),
    (r'/api/v2/([a-zA-Z0-9]+)/videos/share/?$', ShareHandler),
    (r'/api/v2/([a-zA-Z0-9]+)/videos/upload/?$', AWSURLHandler),
    (r'/healthcheck/?$', HealthCheckHandler)
], gzip=True)

def main():
    global server
    signal.signal(signal.SIGTERM, lambda sig, y: sys.exit(-sig))

    server = tornado.httpserver.HTTPServer(application)
    server.listen(options.port)
    tornado.ioloop.IOLoop.current().start()

if __name__ == "__main__":
    utils.neon.InitNeon()
    main()

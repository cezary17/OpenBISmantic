import base64
import io
import json
import os

from fastapi import FastAPI, Request, Response
from fastapi.templating import Jinja2Templates
from starlette.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
import pybis
from openbis_json_parser import parse_dict
from openbis_json_parser.parser import write_ontology

app = FastAPI(
    title='OpenBISmantic API',
    root_path='/openbismantic/',
)
templates = Jinja2Templates(directory='templates')
app.mount("/static", StaticFiles(directory="static"), name="static")


class OpenBISmanticResponse(Response):
    media_type = 'application/ld+json'

    def __init__(self, *args, request=None, status_code=200, **kwargs):
        self.request = request
        accept_header = request.headers.get('accept', '').split(',')
        if 'text/html' in accept_header:
            self.media_type = 'text/html'
        elif 'application/x-turtle' in accept_header:
            self.media_type = 'application/x-turtle'
        elif 'application/ld+json' in accept_header:
            self.media_type = 'application/ld+json'
        elif 'application/rdf+xml' in accept_header:
            self.media_type = 'application/rdf+xml'
        elif 'application/json' in accept_header:
            self.media_type = 'application/json'
        super().__init__(*args, **kwargs)

    def render(self, content):
        if self.media_type == 'application/json':
            return json.dumps(content)
        onto = parse_dict(content, base_url=os.environ.get('BASE_URL', None))
        if self.media_type == 'text/html':
            template = templates.get_template('app.html')
            return template.render({'request': self.request, 'onto': onto})
        out_file = io.BytesIO()
        export_format = {
            'application/x-turtle': 'turtle',
            'application/ld+json': 'json-ld',
            'application/rdf+xml': 'xml'
        }.get(self.media_type, 'json-ld')
        write_ontology(onto, out_file, export_format)
        out_file.seek(0)
        return out_file.read()


default_responses = {
    200: {
        "description": "",
        "content": {
            "application/ld+json": {},
            "application/x-turtle": {},
            "application/rdf+xml": {},
            "application/json": {},
        }
    }
}


@app.middleware('http')
async def add_pybis_session(request: Request, call_next):
    verify = not bool(os.environ.get('NO_VERIFY_CERTIFICATES', False))
    bis = pybis.Openbis(os.environ.get('OPENBIS_URL'), verify_certificates=verify)
    bis.logout()
    auth_header = request.headers.get('authorization')
    accept_header = request.headers.get('accept', '').split(',')
    if auth_header is not None and ' ' in auth_header:
        auth_type, auth_value = auth_header.split(' ', 1)
        if auth_type.lower() == 'basic':
            un, pw = base64.b64decode(auth_value).decode('utf-8').split(':')
            bis.login(un, pw)  # todo: catch exception?
        elif auth_type.lower() == 'token':
            bis.__dict__['token'] = auth_value
    else:
        session_token = request.cookies.get('openbis')
        if session_token is None or session_token == 'null':
            if 'text/html' in accept_header:
                return RedirectResponse(url='/openbis/webapp/eln-lims/')
        else:
            bis.__dict__['token'] = session_token
    if not bis.is_session_active():
        if 'text/html' in accept_header:
            return RedirectResponse(url='/openbis/webapp/eln-lims/')
        else:
            return Response('unauthorized', status_code=401)
    request.state.bis = bis
    response = await call_next(request)
    return response


@app.get('/')
async def index(request: Request):
    return templates.TemplateResponse('index.html', {'request': request})


@app.get('/test')
async def index(request: Request):
    return templates.TemplateResponse('index2.html', {'request': request})


@app.get('/object/{perm_id}', response_class=OpenBISmanticResponse, responses=default_responses)
async def get_object(perm_id: str, request: Request):
    bis: pybis.Openbis = request.state.bis
    try:
        sample = bis.get_sample(perm_id, only_data=True)
    except ValueError:
        return Response('object not found', status_code=404)
    return OpenBISmanticResponse(sample, request=request)


@app.get('/collection/{perm_id}', response_class=OpenBISmanticResponse, responses=default_responses)
async def get_collection(perm_id: str, request: Request):
    bis: pybis.Openbis = request.state.bis
    try:
        collection = bis.get_experiment(perm_id, only_data=True)
    except ValueError:
        return Response('collection not found', status_code=404)
    return OpenBISmanticResponse(collection, request=request)


@app.get('/project/{perm_id}', response_class=OpenBISmanticResponse, responses=default_responses)
async def get_project(perm_id: str, request: Request):
    bis: pybis.Openbis = request.state.bis
    try:
        req = bis._create_get_request(
            "getProjects",
            "project",
            perm_id,
            ["space", "registrator", "modifier", "attachments"],
            "as.dto.project.fetchoptions.ProjectFetchOptions",
        )
        req['params'][2]['experiments'] = {'@type': 'as.dto.experiment.fetchoptions.ExperimentFetchOptions'}
        res = bis._post_request(bis.as_v3, req)
        project = res[perm_id]
    except (ValueError, IndexError):
        return Response('project not found', status_code=404)
    return OpenBISmanticResponse(project, request=request)


@app.get('/instance/', response_class=OpenBISmanticResponse, responses=default_responses)
async def get_projects(request: Request):
    bis: pybis.Openbis = request.state.bis
    bis.get_spaces()
    try:
        req = {
            "method": pybis.pybis.get_method_for_entity("space", "search"),
            "params": [
                bis.token,
                pybis.pybis._subcriteria_for_code(None, "space"),
                {
                    "@type": "as.dto.space.fetchoptions.SpaceFetchOptions",
                    "registrator": pybis.pybis.get_fetchoption_for_entity("registrator"),
                    "projects": pybis.pybis.get_fetchoption_for_entity("project"),
                },
            ],
        }
        resp = bis._post_request(bis.as_v3, req)
        spaces = resp['objects']
        # server_info = bis.get_server_information()._info
    except (ValueError, IndexError):
        return Response('no spaces found', status_code=404)
    return OpenBISmanticResponse(spaces, request=request)


@app.get('/space/{perm_id}', response_class=OpenBISmanticResponse, responses=default_responses)
async def get_space(perm_id: str, request: Request):
    bis: pybis.Openbis = request.state.bis
    try:
        req = {
            "method": pybis.pybis.get_method_for_entity("space", "get"),
            "params": [
                bis.token,
                [{"permId": perm_id, "@type": "as.dto.space.id.SpacePermId"}],
                {
                    "@type": "as.dto.space.fetchoptions.SpaceFetchOptions",
                    "registrator": pybis.pybis.get_fetchoption_for_entity("registrator"),
                    "projects": pybis.pybis.get_fetchoption_for_entity("project"),
                },
            ],
        }
        resp = bis._post_request(bis.as_v3, req)
        space = resp[perm_id]
    except (ValueError, IndexError):
        return Response('space not found', status_code=404)
    return OpenBISmanticResponse(space, request=request)


@app.get('/user/{user_id}', response_class=OpenBISmanticResponse, responses=default_responses)
async def get_user(user_id: str, request: Request):
    bis: pybis.Openbis = request.state.bis
    try:
        user = bis.get_person(user_id, only_data=True)
    except ValueError:
        return Response('user not found', status_code=404)
    return OpenBISmanticResponse(user, request=request)


@app.get('/class/{object_type_code}', response_class=OpenBISmanticResponse, responses=default_responses)  # todo: separate into to /object_type/ and /collection_type/ ?
async def get_class(object_type_code: str, request: Request):
    bis: pybis.Openbis = request.state.bis
    try:
        object_type = bis.get_object_type(object_type_code, only_data=True)
    except ValueError:
        try:
            object_type = bis.get_collection_type(object_type_code, only_data=True)
        except ValueError:
            return Response('object type not found', status_code=404)
    return OpenBISmanticResponse(object_type, request=request)


@app.get('/object_property/{property_type_code}', response_class=OpenBISmanticResponse, responses=default_responses)
async def get_object_property(property_type_code: str, request: Request):
    bis: pybis.Openbis = request.state.bis
    try:
        property_type = bis.get_property_type(property_type_code, only_data=True)
    except ValueError:
        if not property_type_code.startswith('$'):
            return await get_object_property('$' + property_type_code, request)
        return Response('property type not found', status_code=404)
    return OpenBISmanticResponse(property_type, request=request)


@app.get('/search/', response_class=OpenBISmanticResponse, responses=default_responses)
async def get_search_results(query: str, request: Request):
    bis: pybis.Openbis = request.state.bis
    sample_fetch_options = pybis.pybis.get_fetchoption_for_entity('sample')
    options = [
        "tags",
        "properties",
        "attachments",
        "space",
        "experiment",
        "registrator",
        "modifier",
        "dataSets",
        "project",
    ]
    for option in options:
        sample_fetch_options[option] = pybis.pybis.get_fetchoption_for_entity(option)
    for key in ["parents", "children", "container", "components"]:
        sample_fetch_options[key] = {"@type": "as.dto.sample.fetchoptions.SampleFetchOptions"}
    print(f'{sample_fetch_options=}')
    req = {
        'method': 'searchGlobally',
        'params': [
            bis.token,
            {
                '@type': 'as.dto.global.search.GlobalSearchCriteria',
                'criteria': {
                    '@type': 'as.dto.global.search.GlobalSearchTextCriteria',
                    'fieldName': 'anything',
                    'fieldType': 'ANY_FIELD',
                    'fieldValue': {
                        '@type': 'as.dto.common.search.StringContainsValue',
                        'value': query
                    },
                },
                'operator': 'OR'
            },
            {
                '@type': 'as.dto.global.fetchoptions.GlobalSearchObjectFetchOptions',
                'sample': sample_fetch_options,
                'experiment': pybis.pybis.get_fetchoption_for_entity('experiment'),
            }
        ]
    }
    resp = bis._post_request(bis.as_v3, req)
    items = []
    for obj in resp['objects']:
        for key in ['sample', 'experiment', 'dataSet']:
            if obj.get(key, None):
                items.append(obj[key])
    return OpenBISmanticResponse(items, request=request)

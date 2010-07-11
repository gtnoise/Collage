import flickrapi
import urllib
import re
import sys
import os
import StringIO
import sqlite3
import tempfile
from optparse import OptionParser

from django.http import HttpResponse, HttpResponseRedirect, HttpResponseBadRequest
from django.shortcuts import render_to_response
from django.conf import settings

import Image

from collage_donation.client.rpc import submit, update_attributes

import pdb

script_path = os.path.dirname(sys.argv[0])
def template_path(name):
    return os.path.join(script_path, 'views', '%s.tpl' % name)

api_key = 'ebc4519ce69a3485469c4509e8038f9f'
api_secret = '083b2c8757e2971f'

flickr = flickrapi.FlickrAPI(api_key, api_secret, store_token=False)

DONATION_SERVER = 'https://127.0.0.1:8000/server.py'
APPLICATION_NAME = 'proxy'
UPLOADS_DIR = os.path.abspath('uploads')

def get_latest_tags():
    pagedata = urllib.urlopen('http://flickr.com/photos/tags').read()
    match = re.search('<p id="TagCloud">(.*?)</p>', pagedata, re.S|re.I)
    block = match.group(1)
    print os.path.abspath(os.path.curdir)
    of = open('tags.tpl', 'w')
    matches = re.finditer(r'<a href=".*?" style="font-size: (?P<size>\d+)px;">(?P<tag>.*?)</a>', block)
    for (idx, match) in enumerate(matches):
        print >>of, '(new YAHOO.widget.Button({ type: "checkbox", label: "%s", id: "check%d", name: "check%d", value: "%s", container: "tagsbox"})).setStyle("font-size", "%spx");' % (match.group('tag'), idx, idx, match.group('tag'), match.group('size'))
    print >>of, 'document.write("<input type=\'hidden\' name=\'numtags\' value=\'%d\'/>");' % (idx+1)
    of.close()

def check_credentials(request):
    if 'token' not in request.session or \
            'userid' not in request.session:
        return False

    token = request.session['token']
    f = flickrapi.FlickrAPI(api_key, api_secret, token=token, store_token=False)
    try:
        f.auth_checkToken()
    except flickrapi.FlickrError:
        return False

    return True

def wait_for_key(key, title, token, tags):
    conn = sqlite3.connect(settings.WAITING_DB)
    cur = conn.execute('''INSERT INTO waiting (key, title, token)
                                       VALUES (?, ?, ?)''',
                       (key, title, token))
    rowid = cur.lastrowid
    for tag in tags:
        conn.execute('INSERT INTO tags (tag, waiting_id) VALUES (?, ?)', (tag, rowid))
    conn.commit()
    conn.close()

def index(request):
    if check_credentials(request):
        return HttpResponseRedirect('/upload')
    else:
        return HttpResponseRedirect('/login')

def login(request):
    return render_to_response('login.tpl', {'login_url': flickr.web_login_url(perms='write')})

def logout(request):
    if 'token' in request.session:
        del request.session['token']
    if 'userid' in request.session:
        del request.session['userid']
    return HttpResponseRedirect('/')

def is_valid_filename(filename):
    return len(filename) > 0 \
            and os.path.exists(filename) \
            and os.path.samefile(UPLOADS_DIR, os.path.dirname(filename))

def upload(request):
    if not check_credentials(request):
        return HttpResponseRedirect('/login')

    print 'Vector ids: %s' % request.REQUEST.get('vector_ids')

    vector_ids = request.REQUEST.get('vector_ids')
    if vector_ids:
        vectors = vector_ids.split(';')
    else:
        vectors = None

    args = {'token': request.session['token'],
            'userid': request.session['userid'],
            'vector_ids': vector_ids,
            'vectors': vectors}

    submit_button = request.REQUEST.get('submit')

    if submit_button is None:
        return render_to_response('upload.tpl', args)

    title = request.REQUEST.get('title')
    vector_ids = request.REQUEST.get('vector_ids')
    numtags = request.REQUEST.get('numtags')
    expiration = request.REQUEST.get('expiration')

    if title is None:
        args['error'] = 'You must enter a title'
        return render_to_response('upload.tpl', args)

    if expiration is None:
        args['expiration'] = 'You must specify an expiration time'
        return render_to_response('upload.tpl', args)

    if vector_ids is None or numtags is None:
        args['error'] = 'Form error'
        return render_to_response('upload.tpl', args)
    
    title = title.strip()
    filenames = vector_ids.split(';')
    for filename in filenames:
        if not is_valid_filename(filename):
            args['error'] = 'You must select photos to upload'
            return render_to_response('upload.tpl', args)

    try:
        numtags = int(numtags.strip())
    except ValueError:
        args['error'] = 'Inconsistent upload state. Please try again.'
        return render_to_response('upload.tpl', args)

    numtags = max(numtags, 200)     # Hard clamp on the number of tags, to prevent DoS
    tags = []
    for idx in range(numtags):
        name = 'check%d' % idx
        if name in request.REQUEST:
            tags.append(request.REQUEST[name])
    if len(tags) < 3:
        args['error'] = 'Please select at least 3 tags from the list'
        return render_to_response('upload.tpl', args)

    try:
        expiration = 60*60*int(expiration.strip())
    except ValueError:
        args['error'] = 'Please enter a valid number of hours'
        return render_to_response('upload.tpl', args)
    attributes = map(lambda tag: ('tag', tag), tags)

    for filename in filenames:
        try:
            vector = open(filename, 'rb').read()
            os.unlink(filename)
            key = submit(DONATION_SERVER, vector, APPLICATION_NAME, attributes, expiration)
        except Exception as e:
            args['error'] = 'Cannot contact upload server. Please try again later.'
            return render_to_response('upload.tpl', args)

        wait_for_key(key, title, request.session['token'], tags)

    args['expiration'] = expiration/(60*60)
    return render_to_response('process.tpl', args)

def upload_file(request):
    vector = request.FILES.get('vector')
    token = request.REQUEST.get('token')
    userid = request.REQUEST.get('userid')

    if vector is None or token is None or userid is None:
        return HttpResponseBadRequest()

    f = flickrapi.FlickrAPI(api_key, api_secret, token=token, store_token=False)
    try:
        f.auth_checkToken()
    except flickrapi.FlickrError:
        return HttpResponseBadRequest()

    response = flickr.people_getInfo(user_id=userid)
    ispro = response.find('person').attrib['ispro'] == '1'

    data = vector.read()

    if ispro:
        vector = data
    else:
        img = Image.open(StringIO.StringIO(data))
        (width, height) = img.size
        ratio = min(1024./width, 768./height)
        if ratio >= 1.0:
            vector = data
        else:
            img = img.resize((int(ratio*width), int(ratio*height)), Image.ANTIALIAS)
            outfile = StringIO.StringIO()
            img.save(outfile, 'JPEG')
            vector = outfile.getvalue()
            outfile.close()

    outf = tempfile.NamedTemporaryFile(suffix='.jpg', prefix='upload', dir=UPLOADS_DIR, delete=False)
    outf.write(vector)
    outf.close()

    return HttpResponse(outf.name)

def thumbnail(request):
    filename = request.REQUEST.get('filename')
    if filename is None or not is_valid_filename(filename):
        return HttpResponseBadRequest()

    img = Image.open(filename)
    img.thumbnail((128, 128))
    outfile = StringIO.StringIO()
    img.save(outfile, 'JPEG')
    return HttpResponse(outfile.getvalue(), 'image/png')

def callback(request):
    frob = request.REQUEST.get('frob')
    if not frob:
        return HttpResponseBadRequest()

    token = flickr.get_token(frob)

    f = flickrapi.FlickrAPI(api_key, api_secret, token=token, store_token=False)
    try:
        response = f.auth_checkToken()
    except flickrapi.FlickrError:
        return HttpResponseRedirect('/login')

    userid = response.find('auth').find('user').attrib['nsid']

    print 'Updating: %s, %s' % (userid, token)

    update_attributes(DONATION_SERVER, 'userid', userid, 'token', token)

    request.session['token'] = token
    request.session['userid'] = userid
    return HttpResponseRedirect('/')

#def main():
#    usage = 'usage: %s [options]'
#    parser = OptionParser(usage=usage)
#    parser.set_defaults(database='waiting_keys.sqlite')
#    parser.add_option('-d', '--database', dest='database', action='store', type='string', help='Waiting keys database')
#    (options, args) = parser.parse_args()
#
#    global wait_db
#    wait_db = options.database
#
#    if len(args) != 0:
#        parser.error('Invalid argument')
#
#    get_latest_tags()
#    
#    cherrypy.config.update({'tools.sessions.on': True})
#    config = { '/static':
#                    { 'tools.staticdir.on' : True,
#                      'tools.staticdir.dir': STATIC_DIR }
#             }
#    cherrypy.quickstart(FlickrWebClient(), config=config)
#
#if __name__ == '__main__':
#    main()
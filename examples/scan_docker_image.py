'''
Created on Apr 11, 2019

@author: kumykov

Docker image layer by layer scan.

This program will download docker image and scan it into Blackduck server layer by layer
Each layer will be scanned as a separate project
Then all layers will be added to an umbrella project as components
This will allow the layers to be reported as part of the whole container or alone.

For a standard container image specification as in

      repository/image-name:version

Main project will be named "repository/image-name" and will have "version" as a version

Sub-projects for layers will be named as
      repository/image-name_layer_1
      repository/image-name_layer_2
      .........

Usage:

scan_docker_image.py [-h] [--cleanup CLEANUP] imagespec

positional arguments:
  imagespec          Container image tag, e.g. repository/imagename:version

optional arguments:
  -h, --help         show this help message and exit
  --cleanup CLEANUP  Delete project hierarchy only. Do not scan

'''

from blackduck.HubRestApi import HubInstance
from pprint import pprint
from sys import argv
import json
import os
import requests
import shutil
import subprocess
import sys
from argparse import ArgumentParser

#hub = HubInstance()

'''
quick and dirty wrapper to process some docker functionality
'''
class DockerWrapper():
   
    def __init__(self, workdir, scratch = True):
        self.workdir = workdir
        self.imagedir = self.workdir + "/container"
        self.imagefile = self.workdir + "/image.tar"
        if scratch:
            if os.path.exists(self.workdir):
                if os.path.isdir(self.workdir):
                    shutil.rmtree(self.workdir)
                else:
                    os.remove(self.workdir)
            os.makedirs(self.workdir, 0o755, True)
            os.makedirs(self.workdir + "/container", 0o755, True)
        self.docker_path = self.locate_docker()
        
    def locate_docker(self):
        os.environ['PATH'] += os.pathsep + '/usr/local/bin'
        args = []
        args.append('/usr/bin/which')
        args.append('docker')
        proc = subprocess.Popen(['which','docker'], stdout=subprocess.PIPE)
        out, err = proc.communicate()
        lines = out.decode().split('\n')
        print(lines)
        if 'docker' in lines[0]:
            return lines[0]
        else:
            raise Exception('Can not find docker executable in PATH.')
        
    def pull_container_image(self, image_name):
        args = []
        args.append(self.docker_path)
        args.append('pull')
        args.append(image_name)
        return subprocess.run(args)
        
    def save_container_image(self, image_name):
        args = []
        args.append(self.docker_path)
        args.append('save')
        args.append('-o')
        args.append(self.imagefile)
        args.append(image_name)
        return subprocess.run(args)
    
    def unravel_container(self):
        args = []
        args.append('tar')
        args.append('xvf')
        args.append(self.imagefile)
        args.append('-C')
        args.append(self.imagedir)
        return subprocess.run(args)
    
    def read_manifest(self):
        filename = self.imagedir + "/manifest.json"
        with open(filename) as fp:
            data = json.load(fp)
        return data
        
    def read_config(self):
        manifest = self.read_manifest()
        configFile = self.imagedir + "/" + manifest[0]['Config']
        with open(configFile) as fp:
            data = json.load(fp)
        return data
 
class Detector():
    def __init__(self, hub):
        self.detecturl = 'https://blackducksoftware.github.io/hub-detect/hub-detect.sh'
        self.baseurl = hub.config['baseurl']
        self.filename = '/tmp/hub-detect.sh'
        self.token=hub.config['api_token']
        self.baseurl=hub.config['baseurl']
        self.download_detect()
        
    def download_detect(self):
        with open(self.filename, "wb") as file:
            response = requests.get(self.detecturl)
            file.write(response.content)

    def detect_run(self, options=['--help']):
        cmd = ['bash']
        cmd.append(self.filename)
        cmd.append('--blackduck.url=%s' % self.baseurl)
        cmd.append('--blackduck.api.token=' + self.token)
        cmd.append('--blackduck.trust.cert=true')
        cmd.extend(options)
        subprocess.run(cmd)

class ContainerImageScanner():
    
    def __init__(self, hub, container_image_name, workdir='/tmp/workdir'):
        self.hub = hub
        self.hub_detect = Detector(hub)
        self.docker = DockerWrapper(workdir)
        self.container_image_name = container_image_name
        cindex = container_image_name.rfind(':')
        if cindex == -1:
            self.image_name = container_image_name
            self.image_version = 'latest'
        else:
            self.image_name = container_image_name[:cindex]
            self.image_version = container_image_name[cindex+1:]
        
    def prepare_container_image(self):
        self.docker.pull_container_image(self.container_image_name)
        self.docker.save_container_image(self.container_image_name)
        self.docker.unravel_container()

    def process_container_image(self):
        self.manifest = self.docker.read_manifest()
        print(self.manifest)
        self.config = self.docker.read_config()
        print (self.config)
        
        self.layers = []
        num = 1
        offset = 0
        for i in self.manifest[0]['Layers']:
            layer = {}
            layer['name'] = self.image_name + "_layer_" + str(num)
            layer['path'] = i
            while self.config['history'][num + offset -1].get('empty_layer', False):
                offset = offset + 1
            layer['command'] = self.config['history'][num + offset - 1]
            self.layers.append(layer)
            num = num + 1
        print (json.dumps(self.layers, indent=4))

    def generate_project_structures(self):
        main_project_release = self.hub.get_or_create_project_version(self.image_name, self.image_version)

        for layer in self.layers:
            parameters = {}
            parameters['description'] = layer['command']['created_by']
            sub_project_release = self.hub.get_or_create_project_version(layer['name'], self.image_version, parameters=parameters)
            self.hub.add_version_as_component(main_project_release, sub_project_release)

    def submit_layer_scans(self):
        for layer in self.layers:
            options = []
            options.append('--detect.project.name={}'.format(layer['name']))
            options.append('--detect.project.version.name="{}"'.format(self.image_version))
            options.append('--detect.blackduck.signature.scanner.disabled=false')
            options.append('--detect.code.location.name={}_{}_code_{}'.format(layer['name'],self.image_version,layer['path']))
            options.append('--detect.source.path={}/{}'.format(self.docker.imagedir, layer['path'].split('/')[0]))
            self.hub_detect.detect_run(options)

    def cleanup_project_structure(self):
        release = self.hub.get_or_create_project_version(self.image_name,self.image_version)
            
        components = self.hub.get_version_components(release)
        
        print (components)
        
        for item in components['items']:
            sub_name = item['componentName']
            sub_version_name = item['componentVersionName']
            sub_release = self.hub.get_or_create_project_version(sub_name, sub_version_name)
            print(self.hub.remove_version_as_component(release, sub_release))
            print(self.hub.delete_project_by_name(sub_name))
        print(self.hub.delete_project_by_name(self.image_name))


def scan_container_image(imagespec):
    
    hub = HubInstance()
    scanner = ContainerImageScanner(hub, imagespec)
    scanner.prepare_container_image()
    scanner.process_container_image()
    scanner.generate_project_structures()
    scanner.submit_layer_scans()


def clean_container_project(imagespec):
    hub = HubInstance()
    scanner = ContainerImageScanner(hub, imagespec)
    scanner.cleanup_project_structure()


def main(argv=None):
    
    if argv is None:
        argv = sys.argv
    else:
        argv.extend(sys.argv)
        
    parser = ArgumentParser()
    parser.add_argument('imagespec', help="Container image tag, e.g.  repository/imagename:version")
    parser.add_argument('--cleanup',default=False, help="Delete project hierarchy only. Do not scan")
    args = parser.parse_args()
    
    hub = HubInstance()

    clean_container_project(args.imagespec)
    if not args.cleanup:
        scan_container_image(args.imagespec)

    
if __name__ == "__main__":
    sys.exit(main())
    

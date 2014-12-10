import sys
sys.path.append('/Library/AutoPkg/autopkglib')
from autopkglib import Processor, ProcessorError

import os
import subprocess
from xml.etree import ElementTree
import re
import FoundationPlist

__all__ = ["MunkiServerImporterLinux"]

class MunkiServerImporterLinux(Processor):
	input_variables = {
		"MUNKISERVER_ADDR": {
			"description": "Root URL of a MunkiServer installation",
			"required": True
		},
		"MUNKISERVER_UNIT": {
			"description": "Unit to upload to on the MunkiServer installation"
			"The default is \"default\" on MunkiServer",
			"required": True
		},
		"MUNKISERVER_USER": {
			"description": "User name on a MunkiServer installation",
			"required": True
		},
		"MUNKISERVER_PASSWORD": {
			"description": "Password for a MunkiServer installation",
			"required": True
		},
		"pkg_path": {
			"required": True,
			"description": "Path to a dmg to import.",
		},
		"munkiimport_pkgname": {
			"required": False,
			"description": "Corresponds to --pkgname option to munkiimport.",
		},
		"munkiimport_appname": {
			"required": False,
			"description": "Corresponds to --appname option to munkiimport.",
		},
		"munkiimport_name": {
			"required": False,
			"description": "Corresponds to the name key in the pkginfo.",
		},
		"pkginfo": {
			"required": False,
			"description": ("Dictionary of pkginfo keys to override in the "
			"generated pkginfo."),
		},
	}
	output_variables = {
		"edit_url" : {
			"description" : ("The MunkiServer URL where the package can be "
			"edited manually.")
		}
	}
	
	cookiejar = None
	def curl(self, url, opt = [], data = {}):
		options = opt[:]
		for k in data:
			if data[k].startswith('<'): # send the < literally, don't load a file from a path
				options.append('--form-string')
			else:
				options.append('-F')
			options.append('%s=%s' % (k, data[k]))

		
		options += ['-s']
		options += ['-b', self.cookiejar]
		options += ['-c', self.cookiejar]
		options += [url]
		return subprocess.check_output(['/usr/bin/curl'] + options)
	
	def munkiserver_login(self):
		self.cookiejar = os.path.join(self.env['RECIPE_CACHE_DIR'], 'cookiejar')
		if os.path.exists(self.cookiejar):
			os.unlink(self.cookiejar)
		
		data = {}
		data['username'] = self.env['MUNKISERVER_USER']
		data['pass'] = self.env['MUNKISERVER_PASSWORD']

		# get the token from the login page
		url = self.env['MUNKISERVER_ADDR'] + '/login'
		resp = self.curl(url)
		xml = ElementTree.fromstring(resp)
		at = xml.find(".//{http://www.w3.org/1999/xhtml}input[@name='authenticity_token']")
		data['authenticity_token'] = at.attrib['value']
		
		# now log in
		url = self.env['MUNKISERVER_ADDR'] + '/create_session'
		resp = self.curl(url, data=data)
		if resp.find('Munki Server: index') <= 0 and resp.find('/dashboard">redirected') <= 0:
			raise ProcessorError('Login to MunkiServer failed')
	
	def munkiserver_version_already_exists(self):
		if not 'version' in self.env:
			return False
		url = self.env['MUNKISERVER_ADDR'] + '/%s/packages/%s/%s' % (self.env["MUNKISERVER_UNIT"], self.env['NAME'], self.env['version'])
		try:
			resp = self.curl(url)
			if '404.html' in resp:
				return False
			self.env['edit_url'] = url
			return True
		except:
			return False
	
	def munkiserver_upload_package(self):
		data = {}
		data['package_file'] = '@' + self.env["pkg_path"]
		data['pkginfo_file'] = '@' + self.env["info_path"]
		data['commit'] = 'Upload'
		if 'munkiimport_pkgname' in self.env:
			data['makepkginfo_options[pkgname]'] = self.env["munkiimport_pkgname"]
		if 'munkiimport_appname' in self.env:
			data['makepkginfo_options[appname]'] = self.env["munkiimport_appname"]
		if not 'munkiimport_pkgname' in self.env: # MunkiServer expects this parameter to exist even if it's empty
			self.env["munkiimport_name"] = ''
		data['makepkginfo_options[name]'] = self.env["munkiimport_name"]
		
		# request the upload form
		url = self.env['MUNKISERVER_ADDR'] + '/%s/packages/add' % (self.env["MUNKISERVER_UNIT"])
		resp = self.curl(url)
		if resp.find('Munki Server: new') <= 0:
			raise ProcessorError('Do not have permission to upload new packages to MunkiServer')
		# extract the CSRF token
		xml = ElementTree.fromstring(resp)
		ct = xml.find(".//{http://www.w3.org/1999/xhtml}meta[@name='csrf-token']")
		options = ['-H', 'X-CSRF-Token: ' + ct.attrib['content']]
		
		# now perform the upload
		url = self.env['MUNKISERVER_ADDR'] + '/%s/packages' % (self.env["MUNKISERVER_UNIT"])
		resp = self.curl(url, options, data)
		# check for error
		xml = ElementTree.fromstring(resp)
		for msg in xml.findall(".//{http://www.w3.org/1999/xhtml}div[@class='message error']"):
			if 'Version has already been taken' in msg.text:
				return False
			raise ProcessorError(msg.text)
		
		# get the redirect URL
		self.env['edit_url'] = re.match('.*You are being.*(http.*)".*redirected.*',resp).group(1)
		if not self.env['edit_url'].endswith('/edit'):
			raise ProcessorError('Invalid edit redirect URL received after upload.')
		
		return True
	
	def munkiserver_edit_package(self):
		if not 'pkginfo' in self.env or len(self.env['pkginfo']) == 0:
			return
			
		# request the edit form
		url = self.env['edit_url']
		resp = self.curl(url)
		if resp.find('Munki Server: edit') <= 0:
			raise ProcessorError('Do not have permission to edit packages in MunkiServer')
		# extract the CSRF token
		xml = ElementTree.fromstring(resp)
		ct = xml.find(".//{http://www.w3.org/1999/xhtml}meta[@name='csrf-token']")
		options = ['-H', 'X-CSRF-Token: ' + ct.attrib['content']]
		
		# compose the list
		data = {}
		for key in self.env["pkginfo"]:
			if key == 'receipts' or key == 'installs' or key == 'raw_tags':
				key += '_plist'
			if hasattr(self.env['pkginfo'][key], "__len__") and not hasattr(self.env['pkginfo'][key], "endswith"):
				# it's a non-scalar type but not a string, so it's a plist
				value = FoundationPlist.writePlistToString(self.env['pkginfo'][key])
			else:
				value = self.env['pkginfo'][key]
			data["package[%s]" % key] = value
		
		# change the parameter
		options += ['-X', 'PUT']
		url = url[:-5]
		resp = self.curl(url, options, data)
		# check for error
		xml = ElementTree.fromstring(resp)
		for msg in xml.findall(".//{http://www.w3.org/1999/xhtml}div[@class='message error']"):
			raise ProcessorError(msg.text)
		
		# get the redirect URL
		if 'version' in self.env["pkginfo"] or 'name' in self.env["pkginfo"]:
			self.env['edit_url'] = re.match('.*You are being.*(http.*)".*redirected.*',resp).group(1)
	
	def main(self):
		if not (self.env["pkg_path"].endswith('.dmg') or self.env["pkg_path"].endswith('.pkg')):
			raise ProcessorError("Only DMGs and PKGs are accepted by MunkiServer.")
		if 'pkginfo' in self.env and 'name' in self.env["pkginfo"]:
			raise ProcessorError('The name key must not be overridden in pkginfo. To override the name, set the munkiimport_name variable instead.')

		self.munkiserver_login()
		if self.munkiserver_version_already_exists():
			self.output('Item %s already exists at %s' % (self.env['NAME'], self.env['edit_url']))
			return
		if not self.munkiserver_upload_package():
			self.output('Item %s already exists' % (self.env['NAME']))
			return
		self.munkiserver_edit_package()

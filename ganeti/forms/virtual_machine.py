# Copyright (C) 2010 Oregon State University et al.
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301,
# USA.
import copy

from django import forms
from django.forms import ValidationError
from django.utils import simplejson

from ganeti import constants
from ganeti.fields import DataVolumeField
from ganeti.models import (Cluster, ClusterUser, Organization,
                           VirtualMachineTemplate, VirtualMachine)
from ganeti.utilities import cluster_default_info, cluster_os_list, contains
from django.utils.translation import ugettext_lazy as _

FQDN_RE = r'(?=^.{1,254}$)(^(?:(?!\d+\.|-)[a-zA-Z0-9_\-]{1,63}(?<!-)\.?)+(?:[a-zA-Z]{2,})$)'


class VirtualMachineForm(forms.ModelForm):
    """
    Parent class that holds all vm clean methods
      and shared form fields.
    """
    memory = DataVolumeField(label=_('Memory'), min_value=100)

    class Meta:
        model = VirtualMachineTemplate

    def clean_hostname(self):
        data = self.cleaned_data
        hostname = data.get('hostname')
        cluster = data.get('cluster')
        if hostname and cluster:
            # Verify that this hostname is not in use for this cluster.  It can
            # only be reused when recovering a VM that failed to deploy.
            #
            # Recoveries are only allowed when the user is the owner of the VM
            try:
                vm = VirtualMachine.objects.get(cluster=cluster, hostname=hostname)

                # detect vm that failed to deploy
                if not vm.pending_delete and vm.template is not None:
                    current_owner = vm.owner.cast()
                    if current_owner == self.owner:
                        data['vm_recovery'] = vm
                    else:
                        msg = _("Owner cannot be changed when recovering a failed deployment")
                        self._errors["owner"] = self.error_class([msg])
                else:
                    raise ValidationError(_("Hostname is already in use for this cluster"))

            except VirtualMachine.DoesNotExist:
                # doesn't exist, no further checks needed
                pass

        return hostname

    def clean_vcpus(self):
        vcpus = self.cleaned_data.get("vcpus")

        if vcpus is None or vcpus < 1:
            self._errors["vcpus"] = self.error_class(
                ["At least one CPU must be present"])
        else:
            return vcpus

    def clean_initrd_path(self):
        data = self.cleaned_data['initrd_path']
        if data and not data.startswith('/') and data != 'no_initrd_path':
            msg = u"%s." % _('This field must start with a "/"')
            self._errors['initrd_path'] = self.error_class([msg])
        return data

    def clean_security_domain(self):
        data = self.cleaned_data['security_domain']
        security_model = self.cleaned_data['security_model']
        msg = None

        if data and security_model != 'user':
            msg = u'%s.' % _(
                'This field can not be set if Security Mode is not set to User')
        elif security_model == 'user':
            if not data:
                msg = u'%s.' % _('This field is required')
            elif not data[0].isalpha():
                msg = u'%s.' % _('This field must being with an alpha character')

        if msg:
            self._errors['security_domain'] = self.error_class([msg])
        return data

    def clean_vnc_x509_path(self):
        data = self.cleaned_data['vnc_x509_path']
        if data and not data.startswith('/'):
            msg = u'%s,' % _('This field must start with a "/"')
            self._errors['vnc_x509_path'] = self.error_class([msg])
        return data


class NewVirtualMachineForm(VirtualMachineForm):
    """
    Virtual Machine Creation form
    """
    pvm_exclude_fields = ('disk_type','nic_type', 'boot_order', 'serial_console',
        'cdrom_image_path')

    empty_field = constants.EMPTY_CHOICE_FIELD
    templates = constants.HV_DISK_TEMPLATES
    nicmodes = constants.HV_NIC_MODES

    owner = forms.ModelChoiceField(queryset=ClusterUser.objects.all(), label=_('Owner'))
    cluster = forms.ModelChoiceField(queryset=Cluster.objects.none(), label=_('Cluster'))
    hypervisor = forms.ChoiceField(required=False, choices=[empty_field])
    hostname = forms.RegexField(label=_('Instance Name'), regex=FQDN_RE,
                            error_messages={
                                'invalid': _('Instance name must be resolvable'),
                            },
                            max_length=255)
    pnode = forms.ChoiceField(label=_('Primary Node'), choices=[empty_field])
    snode = forms.ChoiceField(label=_('Secondary Node'), choices=[empty_field])
    os = forms.ChoiceField(label=_('Operating System'), choices=[empty_field])
    disk_template = forms.ChoiceField(label=_('Disk Template'),
                                      choices=templates)
    disk_size = DataVolumeField(label=_('Disk Size'), min_value=100)
    disk_type = forms.ChoiceField(label=_('Disk Type'), choices=[empty_field])
    nic_mode = forms.ChoiceField(label=_('NIC Mode'), choices=nicmodes)
    nic_type = forms.ChoiceField(label=_('NIC Type'), choices=[empty_field])
    boot_order = forms.ChoiceField(label=_('Boot Device'), choices=[empty_field])

    class Meta:
        model = VirtualMachineTemplate
        exclude = ('template_name')

    def __init__(self, user, initial=None, *args, **kwargs):
        self.user = user
        super(NewVirtualMachineForm, self).__init__(initial, *args, **kwargs)

        cluster = None
        if initial:
            if 'cluster' in initial and initial['cluster']:
                try:
                    cluster = Cluster.objects.get(pk=initial['cluster'])
                except Cluster.DoesNotExist:
                    # defer to clean function to return errors
                    pass
        if cluster is not None:
            # set choices based on selected cluster if given
            oslist = cluster_os_list(cluster)
            nodelist = [str(h) for h in cluster.nodes.values_list('hostname', flat=True)]
            nodes = zip(nodelist, nodelist)
            nodes.insert(0, self.empty_field)
            oslist.insert(0, self.empty_field)
            self.fields['pnode'].choices = nodes
            self.fields['snode'].choices = nodes
            self.fields['os'].choices = oslist

            defaults = cluster_default_info(cluster)
            hv = defaults['hypervisor']
            if defaults['iallocator'] != '' :
                self.fields['iallocator'].initial = True
                self.fields['iallocator_hostname'] = forms.CharField(
                                        initial=defaults['iallocator'],
                                        required=False,
                                        widget = forms.HiddenInput())
            self.fields['vcpus'].initial = defaults['vcpus']
            self.fields['memory'].initial = defaults['memory']
            self.fields['nic_link'].initial = defaults['nic_link']
            self.fields['hypervisor'].choices = defaults['hypervisors']
            self.fields['hypervisor'].initial = hv
            
            if hv == 'kvm':
                self.fields['serial_console'].initial = defaults['serial_console']

            # Set field choices and hypervisor
            if hv == 'kvm' or hv == 'xen-pvm':
                self.fields['root_path'].initial = defaults['root_path']
                self.fields['kernel_path'].initial = defaults['kernel_path']
            if hv == 'kvm' or hv == 'xen-hvm':
                self.fields['nic_type'].choices = defaults['nic_types']
                self.fields['disk_type'].choices = defaults['disk_types']
                self.fields['boot_order'].choices = defaults['boot_devices']
                
                self.fields['nic_type'].initial = defaults['nic_type']
                self.fields['disk_type'].initial = defaults['disk_type']
                self.fields['boot_order'].initial = defaults['boot_order']
            if hv == 'xen-pvm':
                for field in self.pvm_exclude_fields:
                    del self.fields[field]

        # set cluster choices based on the given owner
        if initial and 'owner' in initial and initial['owner']:
            try:
                self.owner = ClusterUser.objects.get(pk=initial['owner']).cast()
            except ClusterUser.DoesNotExist:
                self.owner = None
        else:
            self.owner = None

        # Set up owner and cluster choices.
        if user.is_superuser:
            # Superusers may do whatever they like.
            self.fields['owner'].queryset = ClusterUser.objects.all()
            self.fields['cluster'].queryset = Cluster.objects.all()
        else:
            # Fill out owner choices. Remember, the list of owners is a list
            # of tuple(ClusterUser.id, label). If you put ids from other
            # Models into this, no magical correction will be applied and you
            # will assign permissions to the wrong owner; see #2007.
            owners = [(u'', u'---------')]
            for group in user.groups.all():
                owners.append((group.organization.id, group.name))
            if user.has_any_perms(Cluster, ['admin','create_vm'], False):
                profile = user.get_profile()
                owners.append((profile.id, profile.name))
            self.fields['owner'].choices = owners

            # Set cluster choices.  If an owner has been selected then filter
            # by the owner.  Otherwise show everything the user has access to
            # through themselves or any groups they are a member of
            if self.owner:
                q = self.owner.get_objects_any_perms(Cluster, ['admin','create_vm'])
            else:
                q = user.get_objects_any_perms(Cluster, ['admin','create_vm'])
            self.fields['cluster'].queryset = q

    def clean(self):
        data = self.cleaned_data

        # First things first. Let's do any error-checking and validation which
        # requires combinations of data but doesn't require hitting the DB.

        # Check that, if we are on any disk template but diskless, our
        # disk_size is set and greater than zero.
        if data.get("disk_template") != "diskless":
            if not data.get("disk_size", 0):
                self._errors["disk_size"] = self.error_class(
                    [u"Disk size must be set and greater than zero"])

        pnode = data.get("pnode", '')
        snode = data.get("snode", '')
        iallocator = data.get('iallocator', False)
        iallocator_hostname = data.get('iallocator_hostname', '')
        disk_template = data.get("disk_template")

        # Need to have pnode != snode
        if disk_template == "drbd" and not iallocator:
            if pnode == snode and (pnode != '' or snode != ''):
                # We know these are not in self._errors now
                msg = u"%s." % _("Primary and Secondary Nodes must not match")
                self._errors["pnode"] = self.error_class([msg])

                # These fields are no longer valid. Remove them from the
                # cleaned data.
                del data["pnode"]
                del data["snode"]
        else:
            if "snode" in self._errors:
                del self._errors["snode"]

        # If boot_order = CD-ROM make sure imagepath is set as well.
        boot_order = data.get('boot_order', '')
        image_path = data.get('cdrom_image_path', '')
        if boot_order == 'cdrom':
            if image_path == '':
                msg = u"%s." % _("Image path required if boot device is CD-ROM")
                self._errors["cdrom_image_path"] = self.error_class([msg])
                del data["cdrom_image_path"]

        if iallocator:
            # If iallocator is checked,
            #  don't display error messages for nodes
            if iallocator_hostname != '':
                if 'pnode' in self._errors:
                    del self._errors['pnode']
                if 'snode' in self._errors:
                    del self._errors['snode']
            else:
                msg = u"%s." % _(
                    "Automatic Allocation was selected, but there is no IAllocator available.")
                self._errors['iallocator'] = self.error_class([msg])

        # If there are any errors, exit early.
        if self._errors:
            return data

        # From this point, database stuff is alright.

        owner = self.owner
        if owner:
            if isinstance(owner, (Organization,)):
                grantee = owner.group
            else:
                grantee = owner.user
            data['grantee'] = grantee

        # superusers bypass all permission and quota checks
        if not self.user.is_superuser and owner:
            msg = None

            if isinstance(owner, (Organization,)):
                # check user membership in group if group
                if not grantee.user_set.filter(id=self.user.id).exists():
                    msg = u"%s." % _("User is not a member of the specified group")

            else:
                if not owner.user_id == self.user.id:
                    msg = u"%s." % _("You are not allowed to act on behalf of this user")

            # check permissions on cluster
            if 'cluster' in data:
                cluster = data['cluster']
                if not (owner.has_perm('create_vm', cluster)
                        or owner.has_perm('admin', cluster)):
                    msg = u"%s." % _("Owner does not have permissions for this cluster")

                # check quota
                start = data['start']
                quota = cluster.get_quota(owner)
                if quota.values():
                    used = owner.used_resources(cluster, only_running=True)

                    if (start and quota['ram'] is not None and
                        (used['ram'] + data['memory']) > quota['ram']):
                            del data['memory']
                            q_msg = u"%s" % _("Owner does not have enough ram remaining on this cluster. You may choose to not automatically start the instance or reduce the amount of ram.")
                            self._errors["ram"] = self.error_class([q_msg])

                    if quota['disk'] and used['disk'] + data['disk_size'] > quota['disk']:
                        del data['disk_size']
                        q_msg = u"%s" % _("Owner does not have enough diskspace remaining on this cluster.")
                        self._errors["disk_size"] = self.error_class([q_msg])

                    if (start and quota['virtual_cpus'] is not None and
                        (used['virtual_cpus'] + data['vcpus']) >
                        quota['virtual_cpus']):
                            del data['vcpus']
                            q_msg = u"%s" % _("Owner does not have enough virtual cpus remaining on this cluster. You may choose to not automatically start the instance or reduce the amount of virtual cpus.")
                            self._errors["vcpus"] = self.error_class([q_msg])

            if msg:
                self._errors["owner"] = self.error_class([msg])
                del data['owner']

        pnode = data.get("pnode", '')
        snode = data.get("snode", '')
        iallocator = data.get('iallocator', False)
        iallocator_hostname = data.get('iallocator_hostname', '')
        disk_template = data.get("disk_template")

        # Need to have pnode != snode
        if disk_template == "drbd" and not iallocator:
            if pnode == snode and (pnode != '' or snode != ''):
                # We know these are not in self._errors now
                msg = u"%s." % _("Primary and Secondary Nodes must not match")
                self._errors["pnode"] = self.error_class([msg])

                # These fields are no longer valid. Remove them from the
                # cleaned data.
                del data["pnode"]
                del data["snode"]
        else:
            if "snode" in self._errors:
                del self._errors["snode"]

        # If boot_order = CD-ROM make sure imagepath is set as well.
        boot_order = data.get('boot_order', '')
        image_path = data.get('cdrom_image_path', '')
        if boot_order == 'cdrom':
            if image_path == '':
                msg = u"%s." % _("Image path required if boot device is CD-ROM")
                self._errors["cdrom_image_path"] = self.error_class([msg])
                del data["cdrom_image_path"]

        if iallocator:
            # If iallocator is checked,
            #  don't display error messages for nodes
            if iallocator_hostname != '':
                if 'pnode' in self._errors:
                    del self._errors['pnode']
                if 'snode' in self._errors:
                    del self._errors['snode']
            else:
                msg = u"%s." % _("Automatic Allocation was selected, but there is no \
                      IAllocator available.")
                self._errors['iallocator'] = self.error_class([msg])
        
        # Check options which depend on the the hypervisor type
        hv = data.get('hypervisor')
        disk_type = data.get('disk_type')
        nic_type = data.get('nic_type')

        # Check disk_type
        if (hv == 'kvm' and not (contains(disk_type, constants.KVM_DISK_TYPES) or contains(disk_type, constants.HV_DISK_TYPES))) or \
           (hv == 'xen-hvm' and not (contains(disk_type, constants.HVM_DISK_TYPES) or contains(disk_type, constants.HV_DISK_TYPES))):
            msg = '%s is not a valid option for Disk Template on this cluster.' % disk_type
            self._errors['disk_type'] = self.error_class([msg])
        # Check nic_type
        if (hv == 'kvm' and not (contains(nic_type, constants.KVM_NIC_TYPES) or \
           contains(nic_type, constants.HV_NIC_TYPES))) or \
           (hv == 'xen-hvm' and not contains(nic_type, constants.HV_NIC_TYPES)):
            msg = '%s is not a valid option for Nic Type on this cluster.' % nic_type
            self._errors['nic_type'] = self.error_class([msg])
        # Check boot_order 
        if (hv == 'kvm' and not contains(boot_order, constants.KVM_BOOT_ORDER)) or \
           (hv == 'xen-hvm' and not contains(boot_order, constants.HVM_BOOT_ORDER)):
            msg = '%s is not a valid option for Boot Device on this cluster.' % boot_order
            self._errors['boot_order'] = self.error_class([msg])

        # Always return the full collection of cleaned data.
        return data


def check_quota_modify(form):
    """ method for validating user is within their quota when modifying """
    data = form.cleaned_data
    cluster = form.cluster
    owner = form.owner
    vm = form.vm

    # check quota
    if owner is not None:
        start = data['start']
        quota = cluster.get_quota(owner)
        if quota.values():
            used = owner.used_resources(cluster, only_running=True)

            if (start and quota['ram'] is not None and
                (used['ram'] + data['memory']-vm.ram) > quota['ram']):
                    del data['memory']
                    q_msg = u"%s" % _("Owner does not have enough ram remaining on this cluster. You must reduce the amount of ram.")
                    form._errors["ram"] = form.error_class([q_msg])

            if 'disk_size' in data:
                if quota['disk'] and used['disk'] + data['disk_size'] > quota['disk']:
                    del data['disk_size']
                    q_msg = u"%s" % _("Owner does not have enough diskspace remaining on this cluster.")
                    form._errors["disk_size"] = form.error_class([q_msg])

            if (start and quota['virtual_cpus'] is not None and
                (used['virtual_cpus'] + data['vcpus'] - vm.virtual_cpus) >
                quota['virtual_cpus']):
                    del data['vcpus']
                    q_msg = u"%s" % _("Owner does not have enough virtual cpus remaining on this cluster. You must reduce the amount of virtual cpus.")
                    form._errors["vcpus"] = form.error_class([q_msg])


class ModifyVirtualMachineForm(NewVirtualMachineForm):
    """
    Simple way to modify a virtual machine instance.
    """
    # Fields to be excluded from parent.
    exclude = ('start', 'owner', 'cluster', 'hostname', 'name_check',
        'iallocator', 'iallocator_hostname', 'disk_template', 'pnode', 'snode',\
        'disk_size', 'nic_mode', 'template_name', 'hypervisor')
    # Fields to be excluded if the hypervisor is Xen HVM
    hvm_exclude_fields = ('vnc_tls', 'vnc_x509_path', 'vnc_x509_verify', \
        'kernel_path', 'kernel_args', 'initrd_path', 'root_path', \
        'serial_console', 'disk_cache', 'security_model', 'security_domain', \
        'kvm_flag', 'use_chroot', 'migration_downtime', 'usb_mouse', \
        'mem_path')
    pvm_exclude_fields = ('vnc_tls', 'vnc_x509_path', 'vnc_x509_verify',
        'serial_console', 'disk_cache', 'security_model', 'security_domain',
        'kvm_flag', 'use_chroot', 'migration_downtime', 'usb_mouse',
        'mem_path', 'disk_type', 'boot_order', 'nic_type',  'acpi',
        'use_localtime', 'cdrom_image_path', 'vnc_bind_address',
        )
    # Fields that should be required.
    required = ('vcpus', 'memory')
    non_pvm_required = ('disk_type', 'boot_order', 'nic_type')

    disk_caches = constants.HV_DISK_CACHES
    security_models = constants.HV_SECURITY_MODELS
    kvm_flags = constants.KVM_FLAGS
    usb_mice = constants.HV_USB_MICE

    acpi = forms.BooleanField(label='ACPI', required=False)
    disk_cache = forms.ChoiceField(label='Disk Cache', required=False,
        choices=disk_caches)
    initrd_path = forms.CharField(label='initrd Path', required=False)
    kernel_args = forms.CharField(label='Kernel Args', required=False)
    kvm_flag = forms.ChoiceField(label='KVM Flag', required=False,
        choices=kvm_flags)
    mem_path = forms.CharField(label='Mem Path', required=False)
    migration_downtime = forms.IntegerField(label='Migration Downtime',
        required=False)
    nic_mac = forms.CharField(label='NIC Mac', required=False)
    security_model = forms.ChoiceField(label='Security Model',
        required=False, choices=security_models)
    security_domain = forms.CharField(label='Security Domain', required=False)
    usb_mouse = forms.ChoiceField(label='USB Mouse', required=False,
        choices=usb_mice)
    use_chroot = forms.BooleanField(label='Use Chroot', required=False)
    use_localtime = forms.BooleanField(label='Use Localtime', required=False)
    vnc_bind_address = forms.IPAddressField(label='VNC Bind Address',
        required=False)
    vnc_tls = forms.BooleanField(label='VNC TLS', required=False)
    vnc_x509_path = forms.CharField(label='VNC x509 Path', required=False)
    vnc_x509_verify = forms.BooleanField(label='VNC x509 Verify',
        required=False)

    class Meta:
        model = VirtualMachineTemplate

    # TODO: Need to reference cluster in init...but no reference to cluster.
    def __init__(self, user, cluster, initial=None, *args, **kwargs):
        if initial is not None:
            cp = initial.copy()
            cp.__setitem__('cluster', cluster.id)
            initial = cp
        super(ModifyVirtualMachineForm, self).__init__(user, initial=initial,\
                *args, **kwargs)
        # Remove all fields in the form that are not required to modify the 
        #   instance.
        for field in self.exclude:
            del self.fields[field]

        # Make sure certain fields are required
        for field in self.required:
            self.fields[field].required = True

        # Get hypervisor from passed in cluster
        hv = None
        if cluster and cluster.info and 'default_hypervisor' in cluster.info:
            hv = cluster.info['default_hypervisor']
        if hv == 'xen-hvm':
            for field in self.hvm_exclude_fields:
                del self.fields[field]
        elif hv == 'xen-pvm':
            for field in self.pvm_exclude_fields:
                del self.fields[field]

        for field in self.non_pvm_required:
            if hv != 'xen-pvm':
                self.fields[field].required = True

        
        # No easy way to rename a field, so copy to new field and delete old field
        if 'os' in self.fields and self.fields['os']:
            self.fields['os_name'] = copy.copy(self.fields['os'])
            del self.fields['os']
    
    def clean(self):
        data = self.cleaned_data
        kernel_path = data.get('kernel_path')
        initrd_path = data.get('initrd_path')

        # Makesure if initrd_path is set, kernel_path is aswell
        if initrd_path and not kernel_path:
            msg = u"%s." % _("Kernel Path must be specified along with Initrd Path")
            self._errors['kernel_path'] = self.error_class([msg])
            self._errors['initrd_path'] = self.error_class([msg])
            del data['initrd_path']

        vnc_tls = data.get('vnc_tls')
        vnc_x509_path = data.get('vnc_x509_path')
        vnc_x509_verify = data.get('vnc_x509_verify')

        if not vnc_tls and vnc_x509_path:
            msg = u'%s.' % _('This field can not be set without VNC TLS enabled')
            self._errors['vnc_x509_path'] = self.error_class([msg])
        if vnc_x509_verify and not vnc_x509_path:
            msg = u'%s.' % _('This field is required')
            self._errors['vnc_x509_path'] = self.error_class([msg])

        if self.owner:
            data['start'] = 'reboot' in self.data or self.vm.is_running
            check_quota_modify(self)
            del data['start']

        return data


class ModifyConfirmForm(forms.Form):

    def clean(self):
        raw = self.data['rapi_dict']
        data = simplejson.loads(raw)

        cleaned = self.cleaned_data
        cleaned['rapi_dict'] = data
        cleaned['memory'] = data['memory']
        cleaned['vcpus'] = data['vcpus']
        cleaned['start'] = 'reboot' in data or self.vm.is_running
        check_quota_modify(self)
        
        return cleaned


class MigrateForm(forms.Form):
    """ Form used for migrating a Virtual Machine """
    mode = forms.ChoiceField(choices=constants.MODE_CHOICES)
    cleanup = forms.BooleanField(initial=False, required=False,
                                 label=_("Attempt recovery from failed migration"))


class RenameForm(forms.Form):
    """ form used for renaming a Virtual Machine """
    hostname = forms.RegexField(label=_('Instance Name'), regex=FQDN_RE,
                            error_messages={
                                'invalid': _('Instance name must be resolvable'),
                            },
                            max_length=255, required=True)
    ip_check = forms.BooleanField(initial=True, required=False, label=_('IP Check'))
    name_check = forms.BooleanField(initial=True, required=False, label=_('DNS Name Check'))

    def __init__(self, vm, *args, **kwargs):
        self.vm = vm
        super(RenameForm, self).__init__(*args, **kwargs)

    def clean_hostname(self):
        data = self.cleaned_data
        hostname = data.get('hostname', None)
        if hostname and hostname == self.vm.hostname:
            raise ValidationError(_("The new hostname must be different than the current hostname"))
        return hostname

"""
netwalk
Copyright (C) 2021 NTT Ltd

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""


import glob
import pickle
import pynetbox
import netwalk
from slugify import slugify
import logging
import ipaddress
import os

logger = logging.getLogger(__name__)
logger.setLevel(level=logging.DEBUG)

ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)

logger.addHandler(ch)

nb = pynetbox.api(
    'http://192.168.1.192',
    token=os.getenv('api_key', None)
)


def create_cdp_neighbor(swdata, nb_site, nb_neigh_role, interface, nb_int=None):
    # Create undiscovered CDP neighbors
    try:
        neighbor = swdata.interfaces[interface].neighbors[0]
        assert isinstance(neighbor, dict)
        logger.debug("Parsing neighbor %s on %s ip %s platform %s",
                     neighbor['hostname'], neighbor['remote_int'], neighbor['ip'], neighbor['platform'])

        try:
            vendor, model = neighbor['platform'].split()
        except ValueError:
            model = neighbor['platform']
            vendor = "Unknown"

        nb_manufacturer = nb.dcim.manufacturers.get(slug=slugify(vendor))

        if nb_manufacturer is None:
            nb_manufacturer = nb.dcim.manufacturers.create(
                name=vendor, slug=slugify(vendor))

        nb_device_ap = nb.dcim.devices.get(name=neighbor['hostname'])
        if nb_device_ap is None:
            nb_device_type = nb.dcim.device_types.get(
                slug=slugify(model))
            if nb_device_type is None:
                nb_device_type = nb.dcim.device_types.create(model=model,
                                                             manufacturer=nb_manufacturer.id,
                                                             slug=slugify(model))

                logger.warning("Created device type " +
                               vendor + " " + model)

            logger.info("Creating neighbor %s", neighbor['hostname'])
            nb_device_ap = nb.dcim.devices.create(name=neighbor['hostname'],
                                                  device_role=nb_neigh_role.id,
                                                  device_type=nb_device_type.id,
                                                  site=nb_site.id)

            logger.info("Creating interface %s on neighbor %s",
                        neighbor['remote_int'], neighbor['hostname'])
            nb_interface = nb.dcim.interfaces.create(device=nb_device_ap.id,
                                                     name=neighbor['remote_int'],
                                                     type="1000base-t")

            neighbor['nb_device'] = nb_device_ap
        else:
            if nb_int is not None:
                if nb_int.cable is not None:
                    try:
                        assert nb_int.cable_peer.device.name == neighbor['hostname'].split(".")[0]
                        assert nb_int.cable_peer.name == neighbor['remote_int']
                    except AssertionError:
                        nb_int.cable.delete()

            neighbor['nb_device'] = nb_device_ap

    except (AssertionError, KeyError, IndexError):
        pass


def create_devices_and_interfaces(fabric, nb_access_role, nb_core_role, nb_neigh_role, nb_site):
    # Create devices and interfaces
    site_vlans = nb.ipam.vlans.filter(site_id=nb_site.id)
    vlans_dict = {x.vid: x for x in site_vlans}

    for swname, swdata in fabric.switches.items():
        logger.info("Switch %s", swname)
        nb_device_type = nb.dcim.device_types.get(model=swdata.facts['model'])
        if nb_device_type is None:
            nb_manufacturer = nb.dcim.manufacturers.get(
                slug=slugify(swdata.facts['vendor']))
            if nb_manufacturer is None:
                nb_manufacturer = nb.dcim.manufacturers.create(name=swdata.facts['vendor'],
                                                               slug=slugify(swdata.facts['vendor']))

            nb_device_type = nb.dcim.device_types.create(model=swdata.facts['model'],
                                                         manufacturer=nb_manufacturer.id,
                                                         slug=slugify(swdata.facts['model']))

        nb_device = nb.dcim.devices.get(name=swdata.facts['hostname'])
        if nb_device is None:
            nb_device = nb.dcim.devices.create(name=swdata.facts['hostname'],
                                               device_role=nb_access_role.id,
                                               device_type=nb_device_type.id,
                                               site=nb_site.id,
                                               serial_number=swdata.facts['serial_number'])
        else:
            try:
                assert nb_device.device_type.model == swdata.facts['model']
                assert nb_device.serial == swdata.facts['serial_number']
            except AssertionError:
                logger.warning("Switch %s changed model from %s to %s", swdata.facts['hostname'], nb_device.device_type.display_name, swdata.facts['model'])
                nb_device.update({'device_type': nb_device_type.id,
                                  'serial': swdata.facts['serial_number']})

        nb_all_interfaces = {
            x.name: x for x in nb.dcim.interfaces.filter(device_id=nb_device.id)}

        # Create new interfaces
        for interface in swdata.interfaces.keys():
            intproperties = {}
            if interface not in nb_all_interfaces:
                logger.info("Interface %s on switch %s", interface, swname)
                if "Fast" in interface:
                    int_type = "100base-tx"
                elif "Te" in interface:
                    int_type = "10gbase-x-sfpp"
                elif "Gigabit" in interface:
                    int_type = "1000base-t"
                elif "Vlan" in interface:
                    int_type = "virtual"
                elif "channel" in interface:
                    int_type = "lag"
                else:
                    int_type = 'virtual'

                try:
                    thisint = swdata.interfaces[interface]
                    if thisint.description is not None:
                        intproperties['description'] = thisint.description

                    if thisint.mode == "trunk":
                        if len(thisint.allowed_vlan) == 4094:
                            intproperties['mode'] = "tagged-all"
                        else:
                            intproperties['mode'] = "tagged"
                            intproperties['tagged_vlans'] = [
                                vlans_dict[x].id for x in thisint.allowed_vlan]
                    else:
                        intproperties['mode'] = "access"

                    if "vlan" in interface.lower():
                        vlanid = int(interface.lower().replace("vlan", ""))
                        intproperties['untagged_vlan'] = vlans_dict[vlanid].id
                    else:
                        intproperties['untagged_vlan'] = vlans_dict[thisint.native_vlan].id
                    intproperties['enabled'] = thisint.is_enabled
                except:
                    pass

                nb_interface = nb.dcim.interfaces.create(device=nb_device.id,
                                                         name=interface,
                                                         type=int_type,
                                                         **intproperties)
                create_cdp_neighbor(swdata, nb_site, nb_neigh_role, interface)

            else:
                thisint = swdata.interfaces[interface]
                nb_int = nb_all_interfaces[interface]

                if len(thisint.neighbors) == 0:
                    if nb_int.cable is not None:
                        logger.info("Deleting old cable on %s", thisint.name)
                        nb_int.cable.delete()
                else:
                    create_cdp_neighbor(swdata, nb_site, interface, nb_neigh_role, nb_int=nb_int)

                if thisint.description != nb_int.description:
                    intproperties['description'] = thisint.description if thisint.description is not None else ""

                if thisint.mode == 'trunk':
                    if len(thisint.allowed_vlan) == 4094:
                        try:
                            assert nb_int.mode.value == 'tagged-all'
                        except (AssertionError, AttributeError):
                            intproperties['mode'] = 'tagged-all'
                    else:
                        try:
                            assert nb_int.mode.value == 'tagged'
                        except (AssertionError, AttributeError):
                            intproperties['mode'] = 'tagged'

                elif thisint.mode == 'access':
                    try:
                        assert nb_int.mode.value == 'access'
                    except (AssertionError, AttributeError):
                        intproperties['mode'] = 'access'

                try:
                    assert nb_int.untagged_vlan == vlans_dict[thisint.native_vlan]
                except AssertionError:
                    intproperties['untagged_vlan'] = vlans_dict[thisint.native_vlan]
                except KeyError:
                    logger.error("VLAN %s on interface %s %s does not exist", thisint.native_vlan, thisint.name, thisint.switch.hostname)
                    continue

                if thisint.is_enabled != nb_int.enabled:
                    intproperties['enabled'] = thisint.is_enabled

                if len(intproperties) > 0:
                    logger.info("Updating interface %s on %s",
                                interface, swname)
                    nb_int.update(intproperties)


        # Delete interfaces that no longer exist
        for k, v in nb_all_interfaces.items():
            if k not in swdata.interfaces:
                logger.info("Deleting interface %s from %s", k, swname)
                v.delete()


def add_ip_addresses(fabric, nb_access_role, nb_core_role, nb_neigh_role, nb_site):
    for swname, swdata in fabric.switches.items():
        nb_device = nb.dcim.devices.get(name=swdata.facts['hostname'])
        nb_device_addresses = {ipaddress.ip_interface(
            x): x for x in nb.ipam.ip_addresses.filter(device_id=nb_device.id)}
        nb_device_interfaces = {
            x.name: x for x in nb.dcim.interfaces.filter(device_id=nb_device.id)}
        all_device_addresses = []

        # Cycle through interfaces, see if the IPs on them are configured
        for intname, intdata in swdata.interfaces.items():
            try:
                assert hasattr(intdata, 'address')
                assert len(intdata.address) != 0
            except AssertionError:
                continue

            nb_interface = nb_device_interfaces[intname]

            if 'ipv4' in intdata.address:
                for address, addressdata in intdata.address['ipv4'].items():
                    logger.info("Checking IP %s", str(address))
                    all_device_addresses.append(address)

                    if address not in nb_device_addresses:
                        logger.info("Checking prefix %s", str(address.network))
                        nb_prefix = nb.ipam.prefixes.get(prefix=str(address.network),
                                                         site_id=nb_site.id)

                        if nb_prefix is None:
                            logger.info("Creating prefix %s",
                                        str(address.network))
                            try:
                                nb_prefix = nb.ipam.prefixes.create(prefix=str(address.network),
                                                                    site=nb_site.id,
                                                                    vlan=nb_interface.untagged_vlan.id)
                            except:
                                pass

                        logger.info("Checking IP %s", str(address))
                        nb_address = nb.ipam.ip_addresses.get(address=str(address),
                                                              site_id=nb_site.id)
                        if nb_address is None:
                            logger.info("Creating IP %s", str(address))
                            nb_address = nb.ipam.ip_addresses.create(address=str(address),
                                                                     site=nb_site.id)

                        nb_device_addresses[address] = nb_address

                    nb_address = nb_device_addresses[address]
                    newdata = {}
                    if nb_address.assigned_object_type != 'dcim.interface':
                        newdata['assigned_object_type'] = 'dcim.interface'
                    if nb_address.assigned_object_id != nb_interface.id:
                        newdata['assigned_object_id'] = nb_interface.id

                    role = None if addressdata['type'] == 'primary' else addressdata['type']

                    if nb_address.role != role:
                        newdata['role'] = role

                    if len(newdata) > 0:
                        logger.info("Updating address %s", address)
                        nb_address.update(newdata)

            if 'hsrp' in intdata.address and 'groups' in intdata.address['hsrp']:
                for hsrpgrp, hsrpdata in intdata.address['hsrp']['groups'].items():
                    try:
                        assert 'address' in hsrpdata
                    except AssertionError:
                        continue

                    logger.info("Checking HSRP address %s on %s %s",
                                hsrpdata['address'], intdata.switch.facts['hostname'], intdata.name)

                    # Lookup in 'normal' ips to find out address netmask

                    netmask = None

                    if 'ipv4' in intdata.address:
                        for normal_address, normal_adddressdata in intdata.address['ipv4'].items():
                            if hsrpdata['address'] in normal_address.network:
                                netmask = normal_address.network

                    assert netmask is not None, "Could not find netmask for HSRP address" + \
                        str(hsrpdata['address'])

                    logger.info("Checking address %s", hsrpdata['address'])
                    try:
                        hsrp_addr_obj = ipaddress.ip_interface(
                            str(hsrpdata['address'])+"/" + str(normal_address).split('/')[1])
                        all_device_addresses.append(hsrp_addr_obj)
                        assert hsrp_addr_obj in nb_device_addresses
                    except AssertionError:
                        logger.info("Creating HSRP address %s",
                                    hsrpdata['address'])
                        nb_hsrp_address = nb.ipam.ip_addresses.create(address=str(hsrp_addr_obj),
                                                                      assigned_object_id=nb_interface.id,
                                                                      assigned_object_type='dcim.interface',
                                                                      role='hsrp')
                        nb_device_addresses[hsrp_addr_obj] = nb_hsrp_address

        for k, v in nb_device_addresses.items():
            if k not in all_device_addresses:
                logger.warning("Deleting old address %s from %s", k, swname)
                ip_to_remove = nb.ipam.ip_addresses.get(
                    q=str(k), device_id=nb_device.id)
                ip_to_remove.delete()
            else:
                if nb_device.primary_ip4 != v:
                    if v.assigned_object is not None:
                        if v.assigned_object.name.lower() == "vlan901":
                            if v.role is None:
                                logger.info("Assign %s as primary ip for %s", v, swname)
                                nb_device.update({'primary_ip4': v.id})
                        elif len(swdata.interfaces_ip.items()) == 1:
                            logger.info("Assign %s as primary ip for %s", v, swname)
                            nb_device.update({'primary_ip4': v.id})

 
def add_neighbor_ip_addresses(fabric, nb_access_role, nb_core_role, nb_neigh_role, nb_site):
    for swname, swdata in fabric.switches.items():
        for intname, intdata in swdata.interfaces.items():
            try:
                neighbor = swdata.interfaces[intname].neighbors[0]
                assert isinstance(neighbor, dict)
            except (AssertionError, KeyError, IndexError):
                continue

            try:
                nb_neigh_device = neighbor['nb_device']
            except KeyError:
                nb_neigh_device = nb.dcim.devices.get(
                    name=neighbor['hostname'])

                if nb_neigh_device is None:
                    create_cdp_neighbor(swdata, nb_site, nb_neigh_role, intname)
                    nb_neigh_device = nb.dcim.devices.get(
                                            name=neighbor['hostname'])

            nb_neigh_interface = nb.dcim.interfaces.get(name=neighbor['remote_int'],
                                                        device_id=nb_neigh_device.id)

            try:
                assert nb_neigh_interface is not None
            except AssertionError:
                nb_neigh_interface = nb.dcim.interfaces.create(device=nb_neigh_device.id,
                                                               name=neighbor['remote_int'],
                                                               type="1000base-t")

                logger.info("Creating interface %s for AP %s, model %s",
                            neighbor['remote_int'], neighbor['hostname'], neighbor['platform'])

            # Search IP
            logger.debug("Searching IP %s for %s",
                         neighbor['ip'], neighbor['hostname'])
            nb_neigh_ips = [x for x in nb.ipam.ip_addresses.filter(
                device_id=nb_neigh_device.id)]

            if any([x.assigned_object_id != nb_neigh_interface.id for x in nb_neigh_ips]):
                logger.error(
                    "Error, neighbor device %s has IPs on more interfaces than discovered, is this an error?", neighbor['hostname'])
                continue

            if len(nb_neigh_ips) == 0:
                # No ip found, figure out smallest prefix configured that contains the IP
                logger.debug(
                    "IP %s not found, looking for prefixes", neighbor['ip'])
                nb_prefixes = nb.ipam.prefixes.filter(q=neighbor['ip'])
                if len(nb_prefixes) > 0:
                    # Search smallest prefix
                    prefixlen = 0
                    smallestprefix = None
                    for prefix in nb_prefixes:
                        logger.debug(
                            "Checking prefix %s, longest prefix found so far: %s", prefix['prefix'], smallestprefix)
                        thispref = ipaddress.ip_network(prefix['prefix'])
                        if thispref.prefixlen > prefixlen:
                            prefixlen = thispref.prefixlen
                            logger.debug(
                                "Found longest prefix %s", thispref)
                            smallestprefix = thispref

                    assert smallestprefix is not None

                # Now we have the smallest prefix length we can create the ip address

                    finalip = f"{neighbor['ip']}/{smallestprefix.prefixlen}"
                else:
                    finalip = neighbor['ip'] + "/32"
                logger.debug("Creating IP %s", finalip)
                nb_neigh_ips.append(
                    nb.ipam.ip_addresses.create(address=finalip))

            for nb_neigh_ip in nb_neigh_ips:
                if str(ipaddress.ip_interface(nb_neigh_ip.address).ip) != neighbor['ip']:
                    logger.warning("Deleting old IP %s form %s",
                                   nb_neigh_ip.address, neighbor['hostname'])
                    nb_neigh_ip.delete()

                if nb_neigh_ip.assigned_object_id != nb_neigh_interface.id:
                    logger.debug("Associating IP %s to interface %s",
                                 nb_neigh_ip.address, nb_neigh_interface.name)
                    nb_neigh_ip.update({'assigned_object_type': 'dcim.interface',
                                        'assigned_object_id': nb_neigh_interface.id})

                    nb_neigh_device.update({'primary_ip4': nb_neigh_ip.id})


def add_l2_vlans(fabric, nb_access_role, nb_core_role, nb_neigh_role, nb_site):
    nb_all_vlans = [x for x in nb.ipam.vlans.filter(site_id=nb_site.id)]
    vlan_dict = {x.vid: x for x in nb_all_vlans}
    for swname, swdata in fabric.switches.items():
        for vlanid, vlandata in swdata.vlans.items():
            if int(vlanid) not in vlan_dict:
                logger.info("Adding vlan %s", vlanid)
                nb_vlan = nb.ipam.vlans.create(vid=vlanid,
                                               name=vlandata['name'],
                                               site=nb_site.id)
                vlan_dict[int(vlanid)] = nb_vlan


def add_cables(fabric, nb_access_role, nb_core_role, nb_neigh_role, nb_site):
    logger.info("Adding cables")
    all_nb_devices = {
        x.name: x for x in nb.dcim.devices.filter(site_id=nb_site.id)}
    for swname, swdata in fabric.switches.items():
        swdata.nb_device = all_nb_devices[swdata.facts['hostname']]

    for swname, swdata in fabric.switches.items():
        logger.info("Checking cables for device %s", swname)
        for intname, intdata in swdata.interfaces.items():
            try:
                if isinstance(intdata.neighbors[0], netwalk.Interface):
                    try:
                        assert hasattr(intdata, 'nb_interface')
                    except AssertionError:
                        intdata.nb_interface = nb.dcim.interfaces.get(
                            device_id=swdata.nb_device.id, name=intname)

                    try:
                        assert hasattr(intdata.neighbors[0], 'nb_interface')
                    except AssertionError:
                        intdata.neighbors[0].nb_interface = nb.dcim.interfaces.get(
                            device_id=intdata.neighbors[0].switch.nb_device.id, name=intdata.neighbors[0].name)

                    nb_term_a = intdata.nb_interface
                    nb_term_b = intdata.neighbors[0].nb_interface

                elif isinstance(intdata.neighbors[0], dict):
                    try:
                        assert hasattr(intdata, 'nb_interface')
                    except AssertionError:
                        intdata.nb_interface = nb.dcim.interfaces.get(
                            device_id=swdata.nb_device.id, name=intname)

                    try:
                        assert hasattr(intdata.neighbors[0], 'nb_device')
                    except AssertionError:
                        intdata.neighbors[0]['nb_device'] = all_nb_devices[intdata.neighbors[0]['hostname']]

                    try:
                        assert hasattr(intdata.neighbors[0], 'nb_interface')
                    except AssertionError:
                        intdata.neighbors[0]['nb_interface'] = nb.dcim.interfaces.get(
                            device_id=intdata.neighbors[0]['nb_device'].id, name=intdata.neighbors[0]['remote_int'])

                    nb_term_a = intdata.nb_interface
                    nb_term_b = intdata.neighbors[0]['nb_interface']
                else:
                    continue

                sw_cables = [x for x in nb.dcim.cables.filter(
                                device_id=nb_term_a.device.id)]
                try:
                    for cable in sw_cables:
                        assert nb_term_a != cable.termination_a
                        assert nb_term_a != cable.termination_b
                        assert nb_term_b != cable.termination_a
                        assert nb_term_b != cable.termination_b
                except AssertionError:
                    continue

                sw_cables = [x for x in nb.dcim.cables.filter(
                                device_id=nb_term_b.device.id)]
                try:
                    for cable in sw_cables:
                        assert nb_term_a != cable.termination_a
                        assert nb_term_a != cable.termination_b
                        assert nb_term_b != cable.termination_a
                        assert nb_term_b != cable.termination_b
                except AssertionError:
                    continue

                logger.info("Adding cable")
                nb_cable = nb.dcim.cables.create(termination_a_type='dcim.interface',
                                                 termination_b_type='dcim.interface',
                                                 termination_a_id=nb_term_a.id,
                                                 termination_b_id=nb_term_b.id)
            except IndexError:
                pass

def add_software_versions(fabric):
    for swname, swdata in fabric.switches.items():
        hostname = swname.replace(".veronesi.com", "")
        logger.debug("Looking up %s", hostname)
        thisdev = nb.dcim.devices.get(name=hostname)
        assert thisdev is not None
        if thisdev['custom_fields']['software_version'] != swdata.facts['os_version']:
            logger.info("Updating %s with version %s", hostname, swdata.facts['os_version'])
            thisdev.update({'custom_fields':{'software_version': swdata.facts['os_version']}})
        else:
            logger.info("Skipping %s", hostname)
        

def main(fabric, nb_access_role, nb_core_role, nb_neigh_role, nb_site):
    add_l2_vlans(fabric, nb_access_role, nb_core_role, nb_neigh_role, nb_site)
    create_devices_and_interfaces(fabric, nb_access_role, nb_core_role, nb_neigh_role, nb_site)
    add_ip_addresses(fabric, nb_access_role, nb_core_role, nb_neigh_role, nb_site)
    add_neighbor_ip_addresses(fabric, nb_access_role, nb_core_role, nb_neigh_role, nb_site)
    add_cables(fabric, nb_access_role, nb_core_role, nb_neigh_role, nb_site)
    add_software_versions(fabric)


if __name__ == '__main__':
    for f in glob.glob("bindata/*"):
        sitename = f.replace("bindata/", "").replace(".bin","")
        logger.info("Opening %s", f)
        with open(f, "rb") as bindata:
            fabric = pickle.load(bindata)

        nb_access_role = nb.dcim.device_roles.get(name="Access Switch")
        nb_core_role = nb.dcim.device_roles.get(name="Core Switch")
        nb_neigh_role = nb.dcim.device_roles.get(name="Access Point")
        nb_site = nb.dcim.sites.get(slug=sitename)
        main(fabric, nb_access_role, nb_core_role, nb_neigh_role, nb_site)

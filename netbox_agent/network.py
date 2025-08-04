import logging
import os
import re
from itertools import chain, islice
from pathlib import Path

import netifaces
from netaddr import IPAddress
from packaging import version

from netbox_agent.config import config
from netbox_agent.config import netbox_instance as nb
from netbox_agent.ethtool import Ethtool
from netbox_agent.ipmi import IPMI
from netbox_agent.lldp import LLDP
import glob

VIRTUAL_NET_FOLDER = Path("/sys/devices/virtual/net")


class Network(object):
    def __init__(self, server, *args, **kwargs):
        self.nics = []

        self.server = server
        self.tenant = self.server.get_netbox_tenant()

        self.lldp = LLDP() if config.network.lldp else None
        self.nics = self.scan()
        self.ipmi = None
        self.dcim_choices = {}
        dcim_c = nb.dcim.interfaces.choices()
        for _choice_type in dcim_c:
            key = "interface:{}".format(_choice_type)
            self.dcim_choices[key] = {}
            for choice in dcim_c[_choice_type]:
                self.dcim_choices[key][choice["display_name"]] = choice["value"]

        self.ipam_choices = {}
        ipam_c = nb.ipam.ip_addresses.choices()
        for _choice_type in ipam_c:
            key = "ip-address:{}".format(_choice_type)
            self.ipam_choices[key] = {}
            for choice in ipam_c[_choice_type]:
                self.ipam_choices[key][choice["display_name"]] = choice["value"]

    def get_network_type():
        return NotImplementedError

    def _parse_vlan_config(self):
        vlan_interfaces = []
        path = "/proc/net/vlan/config"

        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            # Skip the first two lines (headers)
            for line in lines[2:]:
                parts = [p.strip() for p in line.strip().split("|")]
                if len(parts) == 3:
                    iface, vlan_id, phys_dev = parts
                    vlan_interfaces.append(
                        {"interface": iface, "vlan_id": int(vlan_id), "physical_device": phys_dev}
                    )

        except FileNotFoundError:
            return vlan_interfaces
        except (OSError, ValueError) as e:
            logging.error("Error parsing VLAN config: %s", e)
        return vlan_interfaces

    def scan(self):
        nics = []
        vlan_interfaces = self._parse_vlan_config()
        for interface in os.listdir("/sys/class/net/"):
            # ignore if it's not a link (ie: bonding_masters etc)
            if not os.path.islink("/sys/class/net/{}".format(interface)):
                continue

            if config.network.ignore_interfaces and re.match(
                config.network.ignore_interfaces, interface
            ):
                logging.debug("Ignore interface {interface}".format(interface=interface))
                continue

            ip_addr = netifaces.ifaddresses(interface).get(netifaces.AF_INET, [])
            ip6_addr = netifaces.ifaddresses(interface).get(netifaces.AF_INET6, [])
            if config.network.ignore_ips:
                for i, ip in enumerate(ip_addr):
                    if re.match(config.network.ignore_ips, ip["addr"]):
                        ip_addr.pop(i)
                for i, ip in enumerate(ip6_addr):
                    if re.match(config.network.ignore_ips, ip["addr"]):
                        ip6_addr.pop(i)

            # netifaces returns a ipv6 netmask that netaddr does not understand.
            # this strips the netmask down to the correct format for netaddr,
            # and remove the interface.
            # ie, this:
            #   {
            #      'addr': 'fe80::ec4:7aff:fe59:ec4a%eno1.50',
            #      'netmask': 'ffff:ffff:ffff:ffff::/64'
            #   }
            #
            # becomes:
            #   {
            #      'addr': 'fe80::ec4:7aff:fe59:ec4a',
            #      'netmask': 'ffff:ffff:ffff:ffff::'
            #   }
            #
            for addr in ip6_addr:
                addr["addr"] = addr["addr"].replace("%{}".format(interface), "")
                addr["mask"] = addr["mask"].split("/")[0]
                ip_addr.append(addr)

            ethtool = Ethtool(interface).parse()
            if (
                config.network.primary_mac == "permanent"
                and ethtool
                and ethtool.get("mac_address")
            ):
                mac = ethtool["mac_address"]
            else:
                mac = open("/sys/class/net/{}/address".format(interface), "r").read().strip()
                if mac == "00:00:00:00:00:00":
                    mac = None
            if mac:
                mac = mac.upper()

            mtu = int(open("/sys/class/net/{}/mtu".format(interface), "r").read().strip())

            vlan = []
            parent_if = None
            # Check if the interface is in the vlan_interfaces list
            for vlan_info in vlan_interfaces:
                if vlan_info["interface"] == interface:
                    vlan = vlan_info["vlan_id"]
                    parent_if = vlan_info["physical_device"]

            bonding = False
            bonding_slaves = []
            if os.path.isdir("/sys/class/net/{}/bonding".format(interface)):
                bonding = True
                bonding_slaves = (
                    open("/sys/class/net/{}/bonding/slaves".format(interface)).read().split()
                )

            bridge = False
            bridge_slaves = []
            if os.path.isdir("/sys/class/net/{}/bridge".format(interface)):
                bridge = True
                # Bridge slaves are assigned by the lower interface

            all_slaves = glob.glob("/sys/class/net/*/brport")
            bridge_slaves = []
            for brport_path in all_slaves:
                try:
                    # The parent directory is the slave interface
                    slave_iface = Path(brport_path).parent.name
                    # The brport symlink points to the bridge interface
                    bridge_iface = Path(brport_path + "/bridge").resolve().name
                    if bridge_iface == interface:
                        if config.network.ignore_interfaces and re.match(
                            config.network.ignore_interfaces, interface
                        ):
                            logging.debug(
                                "Ignore bridge slave interface {interface}".format(
                                    interface=interface
                                )
                            )
                            continue
                        bridge_slaves.append(slave_iface)
                except Exception as e:
                    logging.debug(f"Error processing bridge slave: {e}")

            virtual = (
                not bridge
                and not bonding
                and Path(f"/sys/class/net/{interface}").resolve().parent == VIRTUAL_NET_FOLDER
            )

            nic = {
                "name": interface,
                "mac": mac,
                "ip": [
                    "{}/{}".format(x["addr"], IPAddress(x["mask"]).netmask_bits()) for x in ip_addr
                ]
                if ip_addr
                else None,  # FIXME: handle IPv6 addresses
                "ethtool": ethtool,
                "virtual": virtual,
                "parent_if": parent_if,
                "vlan": vlan,
                "mtu": mtu,
                "bonding": bonding,
                "bonding_slaves": bonding_slaves,
                "bridge": bridge,
                "bridge_slave": bridge_slaves,
            }
            nics.append(nic)
        return nics

    def _set_bonding_interfaces(self):
        bonding_nics = (x for x in self.nics if x["bonding"])
        for nic in bonding_nics:
            bond_int = self.get_netbox_network_card(nic)
            logging.debug("Setting slave interface for {name}".format(name=bond_int.name))
            for slave_int in (
                self.get_netbox_network_card(slave_nic)
                for slave_nic in self.nics
                if slave_nic["name"] in nic["bonding_slaves"]
            ):
                if slave_int.lag is None or slave_int.lag.id != bond_int.id:
                    logging.debug(
                        "Settting interface {name} as slave of {master}".format(
                            name=slave_int.name, master=bond_int.name
                        )
                    )
                    slave_int.lag = bond_int
                    slave_int.save()
        else:
            return False
        return True

    def _set_bridge_interfaces(self):
        bridge_nics = (x for x in self.nics if x["bridge"])
        for nic in bridge_nics:
            bridge_int = self.get_netbox_network_card(nic)
            logging.debug(
                "Setting bridge slave interfaces for {name}".format(name=bridge_int.name)
            )
            for slave_int in (
                self.get_netbox_network_card(slave_nic)
                for slave_nic in self.nics
                if slave_nic["name"] in nic.get("bridge_slave", [])
            ):
                if (
                    not hasattr(slave_int, "bridge")
                    or slave_int.bridge is None
                    or slave_int.bridge.id != bridge_int.id
                ):
                    logging.debug(
                        "Setting interface {name} as bridge slave of {master}".format(
                            name=slave_int.name, master=bridge_int.name
                        )
                    )
                    slave_int.bridge = bridge_int
                    slave_int.save()
        else:
            return False
        return True

    def get_network_cards(self):
        return self.nics

    def get_netbox_network_card(self, nic):
        if config.network.nic_id == "mac" and nic["mac"]:
            interface = self.nb_net.interfaces.get(mac_address=nic["mac"], **self.custom_arg_id)
        else:
            interface = self.nb_net.interfaces.get(name=nic["name"], **self.custom_arg_id)
        return interface

    def get_netbox_network_cards(self):
        return self.nb_net.interfaces.filter(**self.custom_arg_id)

    def get_netbox_type_for_nic(self, nic):
        if self.get_network_type() == "virtualmachine":
            return self.dcim_choices["interface:type"]["Virtual"]

        if nic.get("bonding"):
            return self.dcim_choices["interface:type"]["Link Aggregation Group (LAG)"]

        if nic.get("bridge"):
            return self.dcim_choices["interface:type"]["Bridge"]

        if nic.get("virtual"):
            return self.dcim_choices["interface:type"]["Virtual"]

        if nic.get("ethtool") is None:
            return self.dcim_choices["interface:type"]["Other"]

        max_speed = nic["ethtool"]["max_speed"]
        if max_speed == "-":
            max_speed = nic["ethtool"]["speed"]

        if max_speed == "10000Mb/s":
            if nic["ethtool"]["port"] in ("FIBRE", "Direct Attach Copper"):
                return self.dcim_choices["interface:type"]["SFP+ (10GE)"]
            return self.dcim_choices["interface:type"]["10GBASE-T (10GE)"]

        elif max_speed == "25000Mb/s":
            if nic["ethtool"]["port"] in ("FIBRE", "Direct Attach Copper"):
                return self.dcim_choices["interface:type"]["SFP28 (25GE)"]

        elif max_speed == "5000Mb/s":
            return self.dcim_choices["interface:type"]["5GBASE-T (5GE)"]

        elif max_speed == "2500Mb/s":
            return self.dcim_choices["interface:type"]["2.5GBASE-T (2.5GE)"]

        elif max_speed == "1000Mb/s":
            if nic["ethtool"]["port"] in ("FIBRE", "Direct Attach Copper"):
                return self.dcim_choices["interface:type"]["SFP (1GE)"]
            return self.dcim_choices["interface:type"]["1000BASE-T (1GE)"]

        return self.dcim_choices["interface:type"]["Other"]

    def get_or_create_vlan(self, vlan_id):
        # FIXME: we may need to specify the datacenter
        # since users may have same vlan id in multiple dc
        vlan = nb.ipam.vlans.get(
            vid=vlan_id,
        )
        if vlan is None:
            vlan = nb.ipam.vlans.create(
                name="VLAN {}".format(vlan_id),
                vid=vlan_id,
            )
        return vlan

    def reset_vlan_on_interface(self, nic, interface):
        update = False
        vlan_id = nic["vlan"]
        lldp_vlan = (
            self.lldp.get_switch_vlan(nic["name"])
            if config.network.lldp and isinstance(self, ServerNetwork)
            else None
        )
        # For strange reason, we need to get the object from scratch
        # The object returned by pynetbox's save isn't always working (since pynetbox 6)
        interface = self.nb_net.interfaces.get(id=interface.id)

        # Handle the case were the local interface isn't an interface vlan as reported by Netbox
        # and that LLDP doesn't report a vlan-id
        if (
            vlan_id is None
            and lldp_vlan is None
            and (interface.mode is not None or len(interface.tagged_vlans) > 0)
        ):
            logging.info(
                "Interface {interface} is not tagged, reseting mode".format(interface=interface)
            )
            update = True
            interface.mode = None
            interface.tagged_vlans = []
            interface.untagged_vlan = None
        # if the local interface is configured with a vlan, it's supposed to be taggued
        # if mode is either not set or not correctly configured or vlan are not
        # correctly configured, we reset the vlan
        elif vlan_id and (
            interface.mode is None
            or type(interface.mode) is not int
            and (
                hasattr(interface.mode, "value")
                and interface.mode.value == self.dcim_choices["interface:mode"]["Access"]
                or len(interface.tagged_vlans) != 1
                or int(interface.tagged_vlans[0].vid) != int(vlan_id)
            )
        ):
            logging.info(
                "Resetting tagged VLAN(s) on interface {interface}".format(interface=interface)
            )
            update = True
            nb_vlan = self.get_or_create_vlan(vlan_id)
            interface.mode = self.dcim_choices["interface:mode"]["Tagged"]
            interface.tagged_vlans = [nb_vlan] if nb_vlan else []
            interface.untagged_vlan = None
        # Finally if LLDP reports a vlan-id with the pvid attribute
        elif lldp_vlan:
            pvid_vlan = [
                key for (key, value) in lldp_vlan.items() if "pvid" in value and value["pvid"]
            ]
            if len(pvid_vlan) > 0 and (
                interface.mode is None
                or interface.mode.value != self.dcim_choices["interface:mode"]["Access"]
                or interface.untagged_vlan is None
                or interface.untagged_vlan.vid != int(pvid_vlan[0])
            ):
                logging.info(
                    "Resetting access VLAN on interface {interface}".format(interface=interface)
                )
                update = True
                nb_vlan = self.get_or_create_vlan(pvid_vlan[0])
                interface.mode = self.dcim_choices["interface:mode"]["Access"]
                interface.untagged_vlan = nb_vlan.id
        return update, interface

    def update_interface_macs(self, nic, macs):
        nb_macs = list(self.nb_net.mac_addresses.filter(interface_id=nic.id))
        # Clean
        for nb_mac in nb_macs:
            if nb_mac.mac_address not in macs:
                logging.debug("Deleting extra MAC {mac} from {nic}".format(mac=nb_mac, nic=nic))
                nb_mac.delete()
        # Add missing
        for mac in macs:
            if mac not in {nb_mac.mac_address for nb_mac in nb_macs}:
                logging.debug("Adding MAC {mac} to {nic}".format(mac=mac, nic=nic))
                self.nb_net.mac_addresses.create(
                    {
                        "mac_address": mac,
                        "assigned_object_type": "dcim.interface",
                        "assigned_object_id": nic.id,
                    }
                )

    def create_netbox_nic(self, nic, mgmt=False):
        # TODO: add Optic Vendor, PN and Serial
        nic_type = self.get_netbox_type_for_nic(nic)
        logging.info(
            "Creating NIC {name} ({mac}) on {device}".format(
                name=nic["name"], mac=nic["mac"], device=self.device.name
            )
        )

        nb_vlan = None

        params = dict(self.custom_arg)
        params.update(
            {
                "name": nic["name"],
                "type": nic_type,
                "mgmt_only": mgmt,
            }
        )
        if nic["mac"]:
            params["mac_address"] = nic["mac"]

        if nic["mtu"]:
            params["mtu"] = nic["mtu"]

        if nic.get("ethtool") and nic["ethtool"].get("link") == "no":
            params["enabled"] = False

        interface = self.nb_net.interfaces.create(**params)

        if nic["vlan"]:
            nb_vlan = self.get_or_create_vlan(nic["vlan"])
            interface.mode = self.dcim_choices["interface:mode"]["Tagged"]
            interface.tagged_vlans = [nb_vlan.id]
            interface.save()
        elif config.network.lldp and self.lldp.get_switch_vlan(nic["name"]) is not None:
            # if lldp reports a vlan on an interface, tag the interface in access and set the vlan
            # report only the interface which has `pvid=yes` (ie: lldp.eth3.vlan.pvid=yes)
            # if pvid is not present, it'll be processed as a vlan tagged interface
            vlans = self.lldp.get_switch_vlan(nic["name"])
            for vid, vlan_infos in vlans.items():
                nb_vlan = self.get_or_create_vlan(vid)
                if vlan_infos.get("vid"):
                    interface.mode = self.dcim_choices["interface:mode"]["Access"]
                    interface.untagged_vlan = nb_vlan.id
            interface.save()

        # cable the interface
        if config.network.lldp and isinstance(self, ServerNetwork):
            switch_ip = self.lldp.get_switch_ip(interface.name)
            switch_interface = self.lldp.get_switch_port(interface.name)

            if switch_ip and switch_interface:
                nic_update, interface = self.create_or_update_cable(
                    switch_ip, switch_interface, interface
                )
                if nic_update:
                    interface.save()
        return interface

    def create_or_update_netbox_ip_on_interface(self, ip, interface):
        """
        Two behaviors:
        - Anycast IP
        * If IP exists and is in Anycast, create a new Anycast one
        * If IP exists and isn't assigned, take it
        * If server is decomissioned, then free IP will be taken

        - Normal IP (can be associated only once)
        * If IP doesn't exist, create it
        * If IP exists and isn't assigned, take it
        * If IP exists and interface is wrong, change interface
        """
        netbox_ips = nb.ipam.ip_addresses.filter(
            address=ip,
        )
        if not netbox_ips:
            logging.info("Create new IP {ip} on {interface}".format(ip=ip, interface=interface))
            query_params = {
                "address": ip,
                "status": "active",
                "assigned_object_type": self.assigned_object_type,
                "assigned_object_id": interface.id,
            }

            netbox_ip = nb.ipam.ip_addresses.create(**query_params)
            return netbox_ip

        netbox_ip = list(netbox_ips)[0]
        # If IP exists in anycast
        if netbox_ip.role and netbox_ip.role.label == "Anycast":
            logging.debug("IP {} is Anycast..".format(ip))
            unassigned_anycast_ip = [x for x in netbox_ips if x.interface is None]
            assigned_anycast_ip = [
                x for x in netbox_ips if x.interface and x.interface.id == interface.id
            ]
            # use the first available anycast ip
            if len(unassigned_anycast_ip):
                logging.info("Assigning existing Anycast IP {} to interface".format(ip))
                netbox_ip = unassigned_anycast_ip[0]
                netbox_ip.interface = interface
                netbox_ip.save()
            # or if everything is assigned to other servers
            elif not len(assigned_anycast_ip):
                logging.info("Creating Anycast IP {} and assigning it to interface".format(ip))
                query_params = {
                    "address": ip,
                    "status": "active",
                    "role": self.ipam_choices["ip-address:role"]["Anycast"],
                    "tenant": self.tenant.id if self.tenant else None,
                    "assigned_object_type": self.assigned_object_type,
                    "assigned_object_id": interface.id,
                }
                netbox_ip = nb.ipam.ip_addresses.create(**query_params)
            return netbox_ip
        else:
            assigned_object = getattr(netbox_ip, "assigned_object", None)
            if not assigned_object:
                logging.info(
                    "Assigning existing IP {ip} to {interface}".format(ip=ip, interface=interface)
                )
            elif assigned_object.id != interface.id:
                old_interface = getattr(netbox_ip, "assigned_object", "n/a")
                logging.info(
                    "Detected interface change for ip {ip}: old interface is "
                    "{old_interface} (id: {old_id}), new interface is {new_interface} "
                    " (id: {new_id})".format(
                        old_interface=old_interface,
                        new_interface=interface,
                        old_id=netbox_ip.id,
                        new_id=interface.id,
                        ip=netbox_ip.address,
                    )
                )
            else:
                return netbox_ip

            netbox_ip.assigned_object_type = self.assigned_object_type
            netbox_ip.assigned_object_id = interface.id
            netbox_ip.save()

    def _nic_identifier(self, nic):
        if isinstance(nic, dict):
            if config.network.nic_id == "mac":
                if not nic["mac"]:
                    logging.warning(
                        "%s: MAC not available while trying to use it as the NIC identifier",
                        nic["name"],
                    )
                return nic["mac"]
            return nic["name"]
        else:
            if config.network.nic_id == "mac":
                if not nic.mac_address:
                    logging.warning(
                        "%s: MAC not available while trying to use it as the NIC identifier",
                        nic.name,
                    )
                return nic.mac_address
            return nic.name

    def create_or_update_netbox_network_cards(self):
        if config.update_all is None or config.update_network is None:
            return None
        logging.debug("Creating/Updating NIC...")

        # delete unknown interface
        nb_nics = list(self.get_netbox_network_cards())
        local_nics = [self._nic_identifier(x) for x in self.nics]
        for nic in list(nb_nics):
            if self._nic_identifier(nic) not in local_nics:
                logging.info(
                    "Deleting netbox interface {name} because not present locally".format(
                        name=nic.name
                    )
                )
                nb_nics.remove(nic)
                nic.delete()

        # delete IP on netbox that are not known on this server
        if len(nb_nics):

            def batched(it, n):
                while batch := tuple(islice(it, n)):
                    yield batch

            netbox_ips = []
            for ids in batched((x.id for x in nb_nics), 25):
                netbox_ips += list(nb.ipam.ip_addresses.filter(**{self.intf_type: ids}))

            all_local_ips = list(
                chain.from_iterable([x["ip"] for x in self.nics if x["ip"] is not None])
            )
            for netbox_ip in netbox_ips:
                if netbox_ip.address not in all_local_ips:
                    logging.info(
                        "Unassigning IP {ip} from {interface}".format(
                            ip=netbox_ip.address, interface=netbox_ip.assigned_object
                        )
                    )
                    netbox_ip.assigned_object_type = None
                    netbox_ip.assigned_object_id = None
                    netbox_ip.save()

        # update each nic
        for nic in self.nics:
            interface = self.get_netbox_network_card(nic)

            if not interface:
                logging.info(
                    "Interface {nic} not found, creating..".format(nic=self._nic_identifier(nic))
                )
                interface = self.create_netbox_nic(nic)

            nic_update = 0

            ret, interface = self.reset_vlan_on_interface(nic, interface)
            nic_update += ret

            if nic["name"] != interface.name:
                logging.info(
                    "Updating interface {interface} name to: {name}".format(
                        interface=interface, name=nic["name"]
                    )
                )
                interface.name = nic["name"]
                nic_update += 1

            if version.parse(nb.version) >= version.parse("4.2"):
                # Create MAC objects
                if nic["mac"]:
                    self.update_interface_macs(interface, [nic["mac"]])

            if nic["mac"] and nic["mac"] != interface.mac_address:
                logging.info(
                    "Updating interface {interface} mac to: {mac}".format(
                        interface=interface, mac=nic["mac"]
                    )
                )
                if version.parse(nb.version) < version.parse("4.2"):
                    interface.mac_address = nic["mac"]
                else:
                    # Find the MAC address object and set its id as primary_mac_address
                    mac_objs = list(
                        self.nb_net.mac_addresses.filter(
                            interface_id=interface.id, mac_address=nic["mac"]
                        )
                    )
                    if mac_objs:
                        interface.primary_mac_address = {"id": mac_objs[0].id}
                    else:
                        # Fallback: set MAC address directly if not found
                        interface.primary_mac_address = {"mac_address": nic["mac"]}
                nic_update += 1

            if hasattr(interface, "mtu"):
                if nic["mtu"] != interface.mtu:
                    logging.info(
                        "Interface mtu is wrong, updating to: {mtu}".format(mtu=nic["mtu"])
                    )
                    interface.mtu = nic["mtu"]
                    nic_update += 1

            if not isinstance(self, VirtualMaschineNetwork) and nic.get("ethtool"):
                if (
                    nic["ethtool"]["duplex"] != "-"
                    and interface.duplex != nic["ethtool"]["duplex"].lower()
                ):
                    interface.duplex = nic["ethtool"]["duplex"].lower()
                    nic_update += 1

                if nic["ethtool"]["speed"] != "-":
                    speed = int(
                        nic["ethtool"]["speed"].replace("Mb/s", "000").replace("Gb/s", "000000")
                    )
                    if speed != interface.speed:
                        interface.speed = speed
                        nic_update += 1

            if hasattr(interface, "type"):
                _type = self.get_netbox_type_for_nic(nic)
                if not interface.type or _type != interface.type.value:
                    logging.info("Interface type is wrong, resetting")
                    interface.type = _type
                    nic_update += 1

            if hasattr(interface, "lag") and interface.lag is not None:
                local_lag_int = next(
                    item for item in self.nics if item["name"] == interface.lag.name
                )
                if nic["name"] not in local_lag_int["bonding_slaves"]:
                    logging.info("Interface has no LAG, resetting")
                    nic_update += 1
                    interface.lag = None

            # cable the interface
            if config.network.lldp and isinstance(self, ServerNetwork):
                switch_ip = self.lldp.get_switch_ip(interface.name)
                switch_interface = self.lldp.get_switch_port(interface.name)
                if switch_ip and switch_interface:
                    ret, interface = self.create_or_update_cable(
                        switch_ip, switch_interface, interface
                    )
                    nic_update += ret

            if nic["ip"]:
                # sync local IPs
                for ip in nic["ip"]:
                    self.create_or_update_netbox_ip_on_interface(ip, interface)
            if nic_update > 0:
                interface.save()

        self._set_bonding_interfaces()
        self._set_bridge_interfaces()
        logging.debug("Finished updating NIC!")


class ServerNetwork(Network):
    def __init__(self, server, *args, **kwargs):
        super(ServerNetwork, self).__init__(server, args, kwargs)

        if config.network.ipmi:
            self.ipmi = self.get_ipmi()
        if self.ipmi:
            self.nics.append(self.ipmi)

        self.server = server
        self.device = self.server.get_netbox_server()
        self.nb_net = nb.dcim
        self.custom_arg = {"device": getattr(self.device, "id", None)}
        self.custom_arg_id = {"device_id": getattr(self.device, "id", None)}
        self.intf_type = "interface_id"
        self.assigned_object_type = "dcim.interface"

    def get_network_type(self):
        return "server"

    def get_ipmi(self):
        ipmi = IPMI().parse()
        return ipmi

    def connect_interface_to_switch(self, switch_ip, switch_interface, nb_server_interface):
        logging.info(
            "Interface {} is not connected to switch, trying to connect..".format(
                nb_server_interface.name
            )
        )
        nb_mgmt_ip = nb.ipam.ip_addresses.get(
            address=switch_ip,
        )
        if not nb_mgmt_ip:
            logging.error("Switch IP {} cannot be found in Netbox".format(switch_ip))
            return nb_server_interface

        try:
            nb_switch = nb_mgmt_ip.assigned_object.device
            logging.info(
                "Found a switch in Netbox based on LLDP infos: {} (id: {})".format(
                    switch_ip, nb_switch.id
                )
            )
        except KeyError:
            logging.error(
                "Switch IP {} is found but not associated to a Netbox Switch Device".format(
                    switch_ip
                )
            )
            return nb_server_interface

        switch_interface = self.lldp.get_switch_port(nb_server_interface.name)
        nb_switch_interface = nb.dcim.interfaces.get(
            device_id=nb_switch.id,
            name=switch_interface,
        )
        if nb_switch_interface is None:
            logging.error("Switch interface {} cannot be found".format(switch_interface))
            return nb_server_interface

        logging.info(
            "Found interface {} on switch {}".format(
                switch_interface,
                switch_ip,
            )
        )
        cable = nb.dcim.cables.create(
            a_terminations=[
                {"object_type": "dcim.interface", "object_id": nb_server_interface.id},
            ],
            b_terminations=[
                {"object_type": "dcim.interface", "object_id": nb_switch_interface.id},
            ],
        )
        nb_server_interface.cable = cable
        logging.info(
            "Connected interface {interface} with {switch_interface} of {switch_ip}".format(
                interface=nb_server_interface.name,
                switch_interface=switch_interface,
                switch_ip=switch_ip,
            )
        )
        return nb_server_interface

    def create_or_update_cable(self, switch_ip, switch_interface, nb_server_interface):
        update = False
        if nb_server_interface.cable is None:
            update = True
            nb_server_interface = self.connect_interface_to_switch(
                switch_ip, switch_interface, nb_server_interface
            )
        else:
            nb_sw_int = nb_server_interface.cable.b_terminations[0]
            nb_sw = nb_sw_int.device
            nb_mgmt_int = nb.dcim.interfaces.get(device_id=nb_sw.id, mgmt_only=True)
            nb_mgmt_ip = nb.ipam.ip_addresses.get(interface_id=nb_mgmt_int.id)
            if nb_mgmt_ip is None:
                logging.error(
                    "Switch {switch_ip} does not have IP on its management interface".format(
                        switch_ip=switch_ip,
                    )
                )
                return update, nb_server_interface

            # Netbox IP is always IP/Netmask
            nb_mgmt_ip = nb_mgmt_ip.address.split("/")[0]
            if nb_mgmt_ip != switch_ip or nb_sw_int.name != switch_interface:
                logging.info("Netbox cable is not connected to correct ports, fixing..")
                logging.info(
                    "Deleting cable {cable_id} from {interface} to {switch_interface} of "
                    "{switch_ip}".format(
                        cable_id=nb_server_interface.cable.id,
                        interface=nb_server_interface.name,
                        switch_interface=nb_sw_int.name,
                        switch_ip=nb_mgmt_ip,
                    )
                )
                cable = nb.dcim.cables.get(nb_server_interface.cable.id)
                cable.delete()
                update = True
                nb_server_interface = self.connect_interface_to_switch(
                    switch_ip, switch_interface, nb_server_interface
                )
        return update, nb_server_interface


class VirtualMaschineNetwork(Network):
    def __init__(self, server, *args, **kwargs):
        super(VirtualMaschineNetwork, self).__init__(server, args, kwargs)
        self.server = server
        self.device = self.server.get_netbox_vm()
        self.nb_net = nb.virtualization
        self.custom_arg = {"virtual_machine": getattr(self.device, "id", None)}
        self.custom_arg_id = {"virtual_machine_id": getattr(self.device, "id", None)}
        self.intf_type = "vminterface_id"
        self.assigned_object_type = "virtualization.vminterface"

        dcim_c = nb.virtualization.interfaces.choices()
        for _choice_type in dcim_c:
            key = "interface:{}".format(_choice_type)
            self.dcim_choices[key] = {}
            for choice in dcim_c[_choice_type]:
                self.dcim_choices[key][choice["display_name"]] = choice["value"]

    def get_network_type(self):
        return "virtualmachine"

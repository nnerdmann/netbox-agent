from netbox_agent.config import config
from netbox_agent.config import netbox_instance as nb
from netbox_agent.lshw import LSHW
from netbox_agent.misc import get_vendor, is_tool
from netbox_agent.raid.hp import HPRaid
from netbox_agent.raid.omreport import OmreportRaid
from netbox_agent.raid.storcli import StorcliRaid
import traceback
import pynetbox
import logging
import json
import re
import os


class ModuleInventory:
    """
    Better Inventory items is there, see:
    - https://github.com/netbox-community/netbox/issues/3087
    - https://github.com/netbox-community/netbox/issues/3333

    This class implements modules for:
    * memory
    * cpu
    * raid cards
    * disks
    * nics

    methods that:
    * get local item
    * get netbox item
    * create netbox item
    * update netbox item

    Known issues:
    - no scan of non-raid devices
    - no scan of NVMe devices
    """

    def __init__(self, server, update_expansion=False):
        # self.create_netbox_tags()
        self.server = server
        self.update_expansion = update_expansion
        netbox_server = self.server.get_netbox_server(update_expansion)

        self.device_id = netbox_server.id if netbox_server else None
        self.raid = None
        self.disks = []

        self.lshw = LSHW()

    # def create_netbox_tags(self):
    #     ret = []
    #     for key, tag in INVENTORY_TAG.items():
    #         nb_tag = nb.extras.tags.get(name=tag["name"])
    #         if not nb_tag:
    #             nb_tag = nb.extras.tags.create(
    #                 name=tag["name"],
    #                 slug=tag["slug"],
    #                 comments=tag["name"],
    #             )
    #         ret.append(nb_tag)
    #     return ret

    def find_or_create_manufacturer(self, name):
        if name is None:
            return None

        manufacturer = nb.dcim.manufacturers.get(
            name=name,
        )
        if not manufacturer:
            logging.info("Creating missing manufacturer %s", name)
            manufacturer = nb.dcim.manufacturers.create(
                name=name,
                slug=re.sub("[^A-Za-z0-9]+", "-", name).lower(),
            )

            logging.info("Creating missing manufacturer %s", name)

        return manufacturer

    def get_netbox_module_type(self, part_number="", model=""):
        """
        Get a module type by part number or name.
        If part_number is provided, it will be used to find the module type.
        If name is provided, it will be used to find the module type.
        """
        module_type = None
        module_types = []
        if part_number and model:
            module_types = nb.dcim.module_types.filter(part_number=part_number, model=model)
        elif part_number:
            module_types = nb.dcim.module_types.filter(part_number=part_number)
        elif model:
            module_types = nb.dcim.module_types.filter(model=model)
        if module_types:
            module_type = next(iter(module_types), None)
        if module_type:
            logging.debug(
                "Found module type %s with part number %s and name %s",
                module_type.id,
                module_type.part_number,
                module_type.model,
            )
        return module_type

    def get_netbox_module_bays(self, prefix=""):
        """
        Get all modules bays for a device with a specific prefix.
        """
        return_list = []
        module_bays = nb.dcim.module_bays.filter(device_id=self.device_id)
        for bay in module_bays:
            if bay.name.startswith(prefix):
                return_list.append(bay)
        return return_list

    def get_netbox_modules(self, profile=""):
        """
        Get all modules for a device with a specific profile.
        """
        return_list = []
        modules = nb.dcim.modules.filter(device_id=self.device_id)
        for module in modules:
            if (
                profile != ""
                and module.module_type
                and module.module_type.profile
                and module.module_type.profile.name == profile
            ):
                return_list.append(module)
            elif profile == "":
                return_list.append(module)
        return return_list

    def do_netbox_cpus(self):
        cpus = self.lshw.get_hw_linux("cpu")
        cpu_bays = self.get_netbox_module_bays(prefix="CPU")
        cpu_modules = self.get_netbox_modules(profile="CPU")

        if len(cpu_bays) == 0 or len(cpu_bays) > 0 and len(cpus) > len(cpu_bays):
            logging.error(
                "Not enough module bays for CPUs in device %s, found %d but need %d",
                self.device_id,
                len(cpu_bays),
                len(cpus),
            )
            return

        for cpu in cpus:
            cpu_name = cpu.get("product", "")
            match = re.search(r"([A-Za-z][0-9]-)?\d{4}\s*[A-Za-z]?[0-9]?", cpu_name)
            part_number = match.group(0).replace(" ", "") if match else ""
            existing_cpu = next(
                (c for c in cpu_modules if c.module_type.part_number == part_number), None
            )
            if existing_cpu:
                logging.debug(
                    "A %s CPU already exists in module bay %s",
                    part_number,
                    existing_cpu.module_bay.name,
                )
                cpu_bays.remove(existing_cpu.module_bay)
                cpu_modules.remove(existing_cpu)
                continue

            # Create a new CPU module in NetBox
            module_bay = cpu_bays.pop(0) if cpu_bays else None
            if module_bay:
                module_type = self.get_netbox_module_type(part_number=part_number)
                if not module_type:
                    logging.error("No module type found for CPU part number %s", part_number)
                    continue
                _ = nb.dcim.modules.create(
                    device=self.device_id,
                    module_bay=module_bay.id,
                    module_type=module_type.id,
                    status="active",
                )
                logging.info(
                    "Creating CPU model %s in module bay %s", part_number, module_bay.name
                )

        #     self.create_netbox_cpus()

    def get_raid_cards(self, filter_cards=False):
        raid_class = None
        if self.server.manufacturer in ("Dell", "Huawei"):
            if is_tool("omreport"):
                raid_class = OmreportRaid
            if is_tool("storcli"):
                raid_class = StorcliRaid
        elif self.server.manufacturer in ("HP", "HPE"):
            if is_tool("ssacli"):
                raid_class = HPRaid

        if not raid_class:
            return []

        self.raid = raid_class()

        if filter_cards and config.expansion_as_device and self.server.own_expansion_slot():
            return [
                c for c in self.raid.get_controllers() if c.is_external() is self.update_expansion
            ]
        else:
            return self.raid.get_controllers()

    def do_netbox_raid_cards(self):
        """
        Synchronize RAID cards between the local system and NetBox using modules and module bays.
        Match RAID cards to bays by slot/position or identifier.
        """
        raid_cards = self.get_raid_cards(filter_cards=True)
        raid_bays = self.get_netbox_module_bays(prefix="RAID")
        raid_modules = self.get_netbox_modules(profile="RAID Controller")

        # Build a mapping of bay name (slot) to bay object
        bay_map = {bay.name: bay for bay in raid_bays}

        # Build a mapping of bay name (slot) to existing module
        module_map = {
            m.module_bay.name: m
            for m in raid_modules
            if m.module_bay and hasattr(m.module_bay, "name")
        }

        for raid_card in raid_cards:
            serial = raid_card.get_serial_number()
            product = raid_card.get_product_name()
            bay_name = ""
            slot = raid_card.data.get("Slot", "")
            external = raid_card.data.get("External", False)
            if slot:
                if not external:
                    bay_name = f"RAID.Emb.{int(slot) + 1}.1"
                else:
                    bay_name = f"RAID.Slot.{slot}.1"
            else:
                logging.error("RAID card does not have a PCI Address, skipping: %s", raid_card)
                continue

            bay = bay_map.get(bay_name)
            existing_module = module_map.get(bay_name)

            # If the module bay does not exist yet, create it
            if not bay:
                logging.info("Creating module bay %s for RAID card", bay_name)
                bay = nb.dcim.module_bays.create(
                    device=self.device_id,
                    name=bay_name,
                )
                bay_map[bay_name] = bay

            # If a module exists in this bay but serial does not match, delete it
            if existing_module and getattr(existing_module, "serial", None) != serial:
                logging.info(
                    "Deleting RAID card module with serial %s from bay %s (expected serial %s)",
                    getattr(existing_module, "serial", None),
                    bay_name,
                    serial,
                )
                existing_module.delete()
                existing_module = None

            # If no module exists in this bay, create it
            if not existing_module and bay:
                # Find or create module type for this RAID card
                module_type = self.get_netbox_module_type(model=product)
                if not module_type:
                    logging.error("No module type found for RAID card model %s", product)
                    continue
                _ = nb.dcim.modules.create(
                    device=self.device_id,
                    module_bay=bay.id,
                    module_type=module_type.id,
                    serial=serial,
                    status="active",
                )
                logging.info(
                    "Creating RAID card module with model %s in bay %s", product, bay.name
                )

    def is_virtual_disk(self, disk, raid_devices):
        disk_type = disk.get("type")
        logicalname = disk.get("logicalname")
        description = disk.get("description")
        size = disk.get("size")
        product = disk.get("product")
        if (
            logicalname in raid_devices
            or disk_type is None
            or product is None
            or description is None
        ):
            return True
        non_raid_disks = [
            "MR9361-8i",
        ]

        if (
            logicalname in raid_devices
            or product in non_raid_disks
            or "virtual" in product.lower()
            or "logical" in product.lower()
            or "volume" in description.lower()
            or "dvd-ram" in description.lower()
            or description == "SCSI Enclosure"
            or (size is None and logicalname is None)
        ):
            return True
        return False

    def get_hw_disks(self):
        disks = []

        for raid_card in self.get_raid_cards(filter_cards=True):
            disks.extend(raid_card.get_physical_disks())

        raid_devices = [
            d.get("custom_fields", {}).get("vd_device")
            for d in disks
            if d.get("custom_fields", {}).get("vd_device")
        ]

        for disk in self.lshw.get_hw_linux("storage"):
            if self.is_virtual_disk(disk, raid_devices):
                continue
            d = {
                "SN": disk.get("serial"),
                "Model": disk.get("product"),
                "Type": disk.get("type"),
            }
            if disk.get("vendor"):
                d["Vendor"] = disk["vendor"]
            else:
                d["Vendor"] = get_vendor(disk["product"])
            disks.append(d)

        # remove duplicate serials
        seen = set()
        uniq = [x for x in disks if x["SN"] not in seen and not seen.add(x["SN"])]
        return uniq

    def do_netbox_disks(self):
        """
        Synchronize disks between the local system and NetBox using modules and module bays.
        Match disks to bays by slot/position or logicalname.
        """
        disks = self.get_hw_disks()
        disk_bays = self.get_netbox_module_bays(prefix="Disk")
        disk_modules = self.get_netbox_modules(profile="Hard disk")

        # Build a mapping of bay name (slot) to bay object
        bay_map = {bay.name: bay for bay in disk_bays}

        # Build a mapping of bay name (slot) to existing module
        module_map = {
            m.module_bay.name: m
            for m in disk_modules
            if m.module_bay and hasattr(m.module_bay, "name")
        }

        for disk in disks:
            model = disk.get("Model", "")[8:]
            pd_id = disk.get("custom_fields", {}).get("pd_identifier")
            if pd_id:
                pd_id = pd_id.strip()
            box = 0
            bay = 0
            if pd_id:
                box = pd_id.split(":")[1]
                bay = pd_id.split(":")[2]
            else:
                logging.error("Disk does not have a custom field 'pd_identifier': %s", disk)
                continue
            module_type = self.get_netbox_module_type(model=model)
            if not module_type:
                logging.error("No module type found for model %s", model)
                continue
            bay_name = "Disk Box " + box + " Bay " + bay

            bay = bay_map.get(bay_name)
            existing_module = module_map.get(bay_name)
            # If the module bay does not exist yet, because a server can have several disk boxes, we will create it
            if not bay:
                logging.info("Creating module bay %s for disk box %s", bay_name, box)
                bay = nb.dcim.module_bays.create(
                    device=self.device_id,
                    name=bay_name,
                )
                bay_map[bay_name] = bay

            # If a module exists in this bay but part number does not match, delete it
            elif existing_module and getattr(existing_module.module_type, "model", "") != model:
                logging.info(
                    "Deleting disk with type %s from bay %s (expected type %s)",
                    getattr(existing_module.module_type, "model", ""),
                    bay_name,
                    model,
                )
                existing_module.delete()
                existing_module = None

            # If no module exists in this bay, create it
            if not existing_module and bay:
                _ = nb.dcim.modules.create(
                    device=self.device_id,
                    module_bay=bay.id,
                    module_type=module_type.id,  # Adjust as needed for disk module type
                    status="active",
                )
                logging.info("Creating disk module with type %s in bay %s", model, bay.name)

    def do_netbox_memories(self):
        """
        Synchronize memory modules between the local system and NetBox using modules and module bays.
        Match memory modules to bays by slot/position.
        """
        memories = self.lshw.get_hw_linux("memory")
        memory_bays = self.get_netbox_module_bays(prefix="Memory")
        memory_modules = self.get_netbox_modules(profile="Memory")

        # Build a mapping of bay name (slot) to bay object
        bay_map = {bay.name: bay for bay in memory_bays}

        # Build a mapping of bay name (slot) to existing module
        module_map = {
            m.module_bay.name: m
            for m in memory_modules
            if m.module_bay and hasattr(m.module_bay, "name")
        }

        for memory in memories:
            slot = str(memory.get("slot", ""))
            # memory_name = memory.get("product", "")
            part_number = memory.get("product", "")

            bay = bay_map.get("Memory " + slot)
            existing_module = module_map.get("Memory " + slot)

            # If a module exists in this bay but part number does not match, delete it
            if (
                existing_module
                and getattr(existing_module.module_type, "part_number", "") != part_number
            ):
                logging.info(
                    "Deleting memory module with part number %s from bay %s (expected part number %s)",
                    getattr(existing_module.module_type, "part_number", ""),
                    slot,
                    part_number,
                )
                existing_module.delete()
                existing_module = None

            # If no module exists in this bay, create it
            if not existing_module and bay:
                module_type = self.get_netbox_module_type(part_number=part_number)
                if not module_type:
                    logging.error("No module type found for memory part number %s", part_number)
                    continue
                _ = nb.dcim.modules.create(
                    device=self.device_id,
                    module_bay=bay.id,
                    module_type=module_type.id,
                    status="active",
                )
                logging.info(
                    "Creating memory module with part number %s in bay %s", part_number, bay.name
                )

    def do_netbox_nics(self):
        """
        Synchronize NICs between the local system and NetBox using modules and module bays.
        Match NICs to bays by slot/position or identifier.
        """
        if self.server.manufacturer in ("HP", "HPE"):
            nics = self.server.get_network_cards()
        else:
            return

        nic_bays = self.get_netbox_module_bays(prefix="NIC")
        nic_modules = self.get_netbox_modules(profile="NIC")

        # Build a mapping of bay name (slot) to bay object
        bay_map = {bay.name: bay for bay in nic_bays}
        # Build a mapping of bay name (slot) to existing module
        module_map = {
            m.module_bay.name: m
            for m in nic_modules
            if m.module_bay and hasattr(m.module_bay, "name")
        }

        for nic in nics:
            module_type = self.get_netbox_module_type(model=nic.get("model", ""))
            if not module_type:
                logging.error("No module type found for NIC model %s", nic.get("model", ""))
                continue
            bay_name = nic.get("module_bay", "")
            bay = bay_map.get(bay_name)
            existing_module = module_map.get(bay_name)

            # If the module bay does not exist yet, create it
            if not bay:
                logging.info("Creating module bay %s for NIC", bay_name)
                bay = nb.dcim.module_bays.create(
                    device=self.device_id,
                    name=bay_name,
                    description=nic.get("location", "") if nic.get("location", "") else None,
                    position=nic.get("position", ""),
                )
                bay_map[bay_name] = bay

            # If a module exists in this bay but model does not match, delete it
            if existing_module and getattr(existing_module, "module_type", None) != module_type:
                logging.info(
                    "Deleting NIC model %s from bay %s (expected model %s)",
                    getattr(existing_module, "module_type", None),
                    bay_name,
                    nic.get("model", ""),
                )
                existing_module.delete()
                existing_module = None

            # If no module exists in this bay, create it
            if not existing_module and bay:
                _ = nb.dcim.modules.create(
                    device=self.device_id,
                    module_bay=bay.id,
                    module_type=module_type.id,
                    status="active",
                    serial=nic.get("serial", ""),
                )
                logging.info(
                    "Creating NIC module with model %s in bay %s", nic.get("model", ""), bay.name
                )

    def do_netbox_psus(self):
        """
        Synchronize PSUs between the local system and NetBox using modules and module bays.
        Only delete existing NetBox PSUs if they are not present in the local system.
        """
        psus = self.lshw.get_hw_linux("power")
        psu_bays = self.get_netbox_module_bays(prefix="PSU")
        psu_modules = self.get_netbox_modules(profile="Power supply")

        # Build a set of serials for local PSUs
        local_serials = set(psu.get("serial", "") for psu in psus if psu.get("serial", ""))

        # Delete NetBox PSU modules not present in local system
        for module in psu_modules:
            module_serial = getattr(module, "serial", None)
            if module_serial not in local_serials:
                logging.info(
                    "Deleting PSU module with serial %s not found in local system", module_serial
                )
                module.delete()

        # Remove already used bays by existing modules that match local serials
        for module in psu_modules:
            module_serial = getattr(module, "serial", None)
            if module_serial in local_serials and module.module_bay in psu_bays:
                psu_bays.remove(module.module_bay)

        for psu in psus:
            model = psu.get("product", "")
            serial = psu.get("serial", "")
            module_type = self.get_netbox_module_type(part_number=model)
            if not module_type:
                logging.error("No module type found for PSU model %s", model)
                continue

            # Skip if a module with this serial already exists
            exists = any(getattr(module, "serial", None) == serial for module in psu_modules)
            if exists:
                continue

            # Use the next free bay
            module_bay = psu_bays.pop(0) if psu_bays else None
            if module_bay:
                _ = nb.dcim.modules.create(
                    device=self.device_id,
                    module_bay=module_bay.id,
                    module_type=module_type.id,
                    status="active",
                    serial=serial,
                )
                logging.info("Creating PSU module with model %s in bay %s", model, module_bay.name)
            else:
                logging.error("No free PSU module bay available for PSU model %s", model)

    def create_or_update(self):
        if config.inventory is None or config.update_inventory is None:
            return False
        if self.update_expansion is False:
            self.do_netbox_cpus()
            self.do_netbox_memories()
            self.do_netbox_disks()
            self.do_netbox_raid_cards()
            self.do_netbox_nics()
            self.do_netbox_psus()
        #  self.do_netbox_gpus()
        return

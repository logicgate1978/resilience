from dns_utils import list_matching_record_sets, parse_weight_assignments, resolve_hosted_zone_id
from validations.base import BaseServiceValidator


class DNSValidator(BaseServiceValidator):
    service_name = "dns"

    def _target(self, context):
        target = context.service.get("target")
        if not isinstance(target, dict):
            self.fail(context, "services[].target must be an object.")
        return target

    def verify_record_exists(self, context) -> None:
        target = self._target(context)
        hosted_zone = str(target.get("hosted_zone") or "").strip()
        record_name = str(target.get("record_name") or "").strip()
        record_type = str(target.get("record_type") or "").strip().upper()
        if not hosted_zone or not record_name or not record_type:
            self.fail(context, "services[].target.hosted_zone, record_name, and record_type are required.")

        route53 = context.session.client("route53")
        hosted_zone_id = resolve_hosted_zone_id(route53, hosted_zone)
        rrsets = list_matching_record_sets(
            route53,
            hosted_zone_id=hosted_zone_id,
            record_name=record_name,
            record_type=record_type,
        )
        if not rrsets:
            self.fail(
                context,
                f"no Route 53 record sets matched hosted_zone={hosted_zone}, record_name={record_name}, record_type={record_type}.",
            )

    def verify_value_present(self, context) -> None:
        value = str(context.service.get("value") or "").strip()
        if not value:
            self.fail(context, "services[].value is required.")

    def verify_simple_record_target(self, context) -> None:
        target = self._target(context)
        route53 = context.session.client("route53")
        hosted_zone_id = resolve_hosted_zone_id(route53, str(target.get("hosted_zone") or "").strip())
        rrsets = list_matching_record_sets(
            route53,
            hosted_zone_id=hosted_zone_id,
            record_name=str(target.get("record_name") or "").strip(),
            record_type=str(target.get("record_type") or "").strip().upper(),
        )
        if len(rrsets) != 1:
            self.fail(context, "dns:set-value requires exactly one matching record set.")
        rrset = rrsets[0]
        if rrset.get("SetIdentifier"):
            self.fail(context, "dns:set-value currently supports simple routing records only, not policy records.")
        if rrset.get("AliasTarget"):
            self.fail(context, "dns:set-value currently supports non-alias records only.")

    def verify_weight_targets(self, context) -> None:
        target = self._target(context)
        assignments = parse_weight_assignments(context.service.get("value"))
        route53 = context.session.client("route53")
        hosted_zone_id = resolve_hosted_zone_id(route53, str(target.get("hosted_zone") or "").strip())
        rrsets = list_matching_record_sets(
            route53,
            hosted_zone_id=hosted_zone_id,
            record_name=str(target.get("record_name") or "").strip(),
            record_type=str(target.get("record_type") or "").strip().upper(),
        )
        rrsets_by_identifier = {str(rrset.get("SetIdentifier") or ""): rrset for rrset in rrsets}
        missing = [identifier for identifier in assignments if identifier not in rrsets_by_identifier]
        if missing:
            self.fail(
                context,
                "dns:set-weight could not find matching weighted record(s) for set identifier(s): "
                + ", ".join(sorted(missing)),
            )
        non_weighted = [identifier for identifier, rrset in rrsets_by_identifier.items() if identifier in assignments and "Weight" not in rrset]
        if non_weighted:
            self.fail(
                context,
                "dns:set-weight requires weighted Route 53 records. Missing Weight field for set identifier(s): "
                + ", ".join(sorted(non_weighted)),
            )

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class PublicAssetsHook(BuildHookInterface):
    def initialize(self, version, build_data):
        # Editable wheels must not shadow the source package with static assets.
        if version == "standard":
            build_data["force_include"]["CHANGELOG.md"] = "ablation_workbench/CHANGELOG.md"
            build_data["force_include"]["skills/ablation"] = "ablation_workbench/skills/ablation"

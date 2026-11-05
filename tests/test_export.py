"""openMINDS collection, referenced KG nodes, the KG push and its errors, anatomy and atlas."""

import types
from datetime import date

import fairgraph.openminds.core as fgcore
import odml
import pytest
import yaml
from fairgraph import Collection
from fairgraph.errors import AuthenticationError, AuthorizationError, ResourceExistsError

from conftest import sessions
from fairephys.enrichment.openminds import enrich_document as openminds_enrich
from fairephys.export import kg
from fairephys.export.anatomy import ATLAS_FIELD, Anatomy, atlas_path, load_atlas
from fairephys.export.collection import SCHEMAS_PATH, SchemaBuilder, _load_yaml, build_collection, save_collection
from fairephys.export.kg_lookup import KGResolver
from fairephys.export.templating import fill_template, machine_label
from fairephys.metadata import compile_to_odml, dict_to_section, save_odml


# (class, label, linked nodes per property) of every node of the offline collection of the conftest doc
SNAPSHOT = [
    ("BehavioralProtocol", f"name=Test Task (Doe, {date.today().year})", ()),  # the dataset citation
    ("BiologicalSex", "name=female", ()),
    ("ContactInformation", "-", ()),
    ("ContributionType", "name=data collection", ()),
    ("CustomAnatomicalEntity", "name=M1", (("has_annotations", 1),)),
    ("Dataset", "name=Test dataset", (("authors", 1), ("custodians", 1), ("has_versions", 1))),
    ("DatasetVersion", "short_name=test-ds", (("behavioral_protocols", 1), ("funding", 1), ("keywords", 2),
                                              ("license", 1), ("other_contributions", 1), ("studied_specimens", 1))),
    ("ElectrodeArray", "lookup_label=test-ds_Utah-Array", (("manufacturers", 1),)),
    ("ElectrodeArrayUsage", "lookup_label=test-ds_sub-MonkeyA_ses-ses01_Utah-Array",
     (("anatomical_locations_of_arrays", 1), ("device", 1))),
    ("Funding", "award_number=GRANT-001", (("funder", 1),)),
    ("Laterality", "name=left", ()),
    ("License", "name=Creative Commons Attribution 4.0 International", ()),
    ("Organization", "name=Blackrock", ()),
    ("Organization", "name=Test Foundation", ()),
    ("Organization", "name=Test Uni", ()),
    ("Person", "full_name=Jane Doe", (("affiliations", 1), ("contact_information", 1))),
    ("Protocol", "name=test-ds", ()),
    ("RecordingActivity", "lookup_label=test-ds_sub-MonkeyA_ses-ses01",
     (("devices", 1), ("inputs", 1), ("is_part_of", 1), ("protocols", 1))),
    ("Setup", "name=MySystem", (("has_parts", 1), ("manufacturers", 1))),
    ("Species", "name=Macaca mulatta", ()),
    ("Subject", "lookup_label=test-ds_sub-MonkeyA", (("biological_sex", 1), ("species", 1), ("studied_states", 1))),
    ("SubjectState", "lookup_label=test-ds_sub-MonkeyA_ses-ses01",
     (("age", 1), ("relative_time_indication", 1), ("weight", 1))),
    ("TermSuggestion", "name=ephys", ()),
    ("TermSuggestion", "name=test", ()),
    ("UnitOfMeasurement", "name=day", ()),
    ("UnitOfMeasurement", "name=kilogram", ()),
    ("UnitOfMeasurement", "name=year", ()),
]


LABELS = ("lookup_label", "name", "full_name", "short_name", "award_number", "identifier")


def _summary(collection) -> list[tuple]:
    rows = []
    for node in collection.nodes.values():
        label = next((f"{a}={getattr(node, a)}" for a in LABELS if getattr(node, a, None)), "-")
        links = []
        for p in type(node).properties:
            value = getattr(node, p.name, None)
            items = value if isinstance(value, list) else [value] if value is not None else []
            if n := sum(1 for i in items if hasattr(type(i), "properties")):
                links.append((p.name, n))
        rows.append((type(node).__name__, label, tuple(sorted(links))))
    return sorted(rows)


def _builder(doc, **kwargs):
    return SchemaBuilder(doc, _load_yaml(SCHEMAS_PATH), kg_client=None, **kwargs)


def test_offline_collection_snapshot(doc, tmp_path):
    """The offline collection of the fixture: every node's class, label and link counts, kept by its JSON-LD."""
    _, xml = save_odml(doc, tmp_path / "test")
    collection = build_collection(xml, kg_client=None)
    assert _summary(collection) == SNAPSHOT
    assert (collection.kg_space, collection.output_dir) == (None, xml.parent)
    loaded = Collection()
    loaded.load(str(save_collection(collection, tmp_path / "test.openminds.jsonld")))
    assert _summary(loaded) == SNAPSHOT


def test_registry_driven_fields(make_doc):
    """Statements, release date, cited protocol (a bare DOI as its https://doi.org/ IRI, a citation an error), keywords,
    techniques, DOIs, consortium and Funding match per registry."""
    doc = make_doc({"Behavioral Protocol": {"described in": "10.1234/x"}}, Dataset={
        "doi": "10.1234/abc", "version innovation": "First release.",
        "techniques": ["electromyography", "spike sorting"], "experimental approach": ["electrophysiology"],
        "keywords": ["electrophysiology", "electromyography", "brain"],
        "related publications": ["10.1000/abc", "https://doi.org/10.1000/def", "A. Title. https://doi.org/10.1234/abc.",
                                 "Author, B. Title without a DOI (2020)."],
        "Project": {"name": "P", "description": "Project text", "dataset statement": "Part of P.",
                    "version statement": "Formats used."},
        "Consortium": {"name": "C", "full name": "Consortium Full"}, "Custodian": {"consortium member": True},
        "repository url": "https://gin.g-node.org/Org/Repo_Curated/", "repository type": "git-annex repository"})
    builder = _builder(doc)
    collection = builder.build_all()
    entities = builder.entities
    version = entities["DatasetVersion"]
    assert entities["Dataset"].description == "A test experiment\n\nPart of P."
    assert version.description is None  # no usage statement: the version inherits the Dataset's description
    assert version.version_innovation == "First release.\n\nFormats used."
    used = _builder(make_doc(Dataset={"Project": {"name": "P", "usage statement": "Read with PyNWB."}}))
    used.build_all()
    assert used.entities["DatasetVersion"].description == "A test experiment\n\nRead with PyNWB."
    assert entities["Project"].description == "Project text"
    assert [t.name for t in version.techniques] == ["electromyography", "spike sorting"]
    assert {t.name for t in version.keywords} == {"brain"}  # the others are the approach and a technique
    protocol = entities["BehavioralProtocol"]
    assert protocol.name == f"Test Task (Doe, {date.today().year})"  # one author: no "et al."
    two = _builder(make_doc(Dataset={"Contributors": [{"given name": "Jane", "family name": "Doe"},
                                                      {"given name": "Ann", "family name": "Roe"}]}))
    assert two.dataset_citation() == f"Doe et al., {date.today().year}"
    released = _builder(make_doc(Dataset={"release date": "2025-09-15"}))
    assert released.dataset_citation() == "Doe, 2025"  # a first publication elsewhere gives its year
    assert released.build_all() and released.entities["DatasetVersion"].release_date == date(2025, 9, 15)
    assert [str(r.iri.value) for r in protocol.described_in] == ["https://doi.org/10.1234/x"]
    with pytest.raises(ValueError, match="Not an IRI"):  # a citation in `described in` is not dropped silently
        _builder(make_doc({"Behavioral Protocol": {"described in": "Author et al., 2026"}})).build_all()
    assert sorted(n.identifier for n in collection.nodes.values() if isinstance(n, fgcore.DOI)) == [
        "https://doi.org/10.1000/abc", "https://doi.org/10.1000/def", "https://doi.org/10.1234/abc"]
    custodian = entities["Custodian"]
    assert [a.member_of.full_name for a in custodian.affiliations] == ["Test Uni", "Consortium Full"]
    assert custodian.affiliations[0].member_of.short_name is None  # the lab is no organization name
    repository = entities["FileRepository"]
    assert (repository.name, repository.type.name) == ("Repo_Curated", "git-annex repository")
    funding, = entities["Funding"]
    assert list(funding._build_existence_query()) == ["award_number"]
    assert fgcore.Funding.existence_query_properties == ("funder",)  # the class is unchanged


def test_subject_state_days_since_the_earliest_session(make_doc):
    """Relative time in days from the subject's earliest dated session (not the first listed); undated: none."""
    doc = make_doc(Sessions=[{"session id": "02", "date": "2024-03-15"}, {"session id": "01", "date": "2024-03-10"},
                             {"session id": "03"}])
    states = _builder(doc).build_schema("SubjectState")
    assert [s.relative_time_indication and s.relative_time_indication.value for s in states] == [5, 0, None]
    assert states[0].relative_time_indication.unit.name == "day"


def test_chronic_implants_give_one_usage_per_session(make_doc):
    """Sessions without placements use the subject's Implanted Probes: one usage per implant and session."""
    doc = make_doc(Subject={"Surgical Preparation": {"Implanted Probes": [
        {"probe": "Utah Array", "brain region": "primary motor cortex", "hemisphere": "left"}]}},
        Sessions=[{"session id": "01", "date": "2024-01-01"}, {"session id": "02", "date": "2024-01-02"}])
    builder = _builder(doc)
    builder.build_all()
    usages = [[(u.lookup_label, u.device.name, u.anatomical_locations_of_arrays[0].related_uberon_term.name)
               for u in a.devices] for a in builder.entities["RecordingActivity"]]
    assert usages == [[("test-ds_sub-MonkeyA_ses-01_Utah-Array", "Utah Array", "primary motor cortex")],
                      [("test-ds_sub-MonkeyA_ses-02_Utah-Array", "Utah Array", "primary motor cortex")]]


def test_template_rendering():
    """Missing tokens give None; empty optional tokens drop with their words and brackets."""
    values = {"description": "Text", "note": "N"}
    cases = [
        ("{description}\n\n{?note}", {}, "Text\n\nN"), ("{description}\n\n{?missing}", {}, "Text"),
        ("{?missing}\n\n{description}", {}, "Text"), ("{description} ({?missing})", {}, "Text"),
        ("A ({?missing}) {description}", {}, "A Text"), ("{description}{?, see {note},} here", {}, "Text, see N, here"),
        ("{description}{?, see {missing},} here", {}, "Text here"), ("{?missing}", {}, None),
        ("{description} {missing}", {}, None), ("{description}\n\n{?note}", {"note": "  "}, "Text"),
        ("{description} {?note}", {"note": ["a", "b"]}, "Text"),
    ]
    for template, override, expected in cases:
        lookup = {**values, **override}.get
        assert fill_template(template, lookup) == expected, template
    assert machine_label(" test-ds_Probe 1 ") == "test-ds_Probe-1"


def _fake_kg(builder, monkeypatch, found: dict):
    """The builder's KG lookup answers from `found` {fairgraph class: node}; the collection accepts anything."""
    monkeypatch.setattr(builder.kg, "lookup_kg", lambda fg_class, *args, **kwargs: found.get(fg_class))
    monkeypatch.setattr(builder.collection, "add", lambda node: None)


def test_referenced_kg_nodes(doc, monkeypatch):
    """Other-space and keep_existing nodes are referenced with differences reported; own ones rebuilt."""
    shared_funding = types.SimpleNamespace(
        id="https://kg.ebrains.eu/api/instances/funding-1", space="common", award_number="GRANT-001",
        award_title="Other title", acknowledgement=None, funder=types.SimpleNamespace(id="org-1"))
    shared_person = types.SimpleNamespace(id="https://kg.ebrains.eu/api/instances/person-1", space="common",
                                          given_name="Jane", family_name="Doe", digital_identifiers=None,
                                          affiliations=[])
    doc["Dataset"]["Contributors"].sections[0].append(dict_to_section("digital identifier", {"type": "ORCID",
                                                                                            "value": "0000-0001"}))
    builder = _builder(doc, target_space="X")
    _fake_kg(builder, monkeypatch, {fgcore.Funding: shared_funding, fgcore.Person: shared_person})
    assert (builder.build_schema("Funding"), builder.build_schema("Person")) == ([shared_funding], [shared_person])
    funding_diff, person_diff = builder.collection.kg_differences
    assert funding_diff.startswith(f"Funding {shared_funding.id} (space 'common'): ")
    assert "award_title is 'Other title' in the KG, 'Test Grant' in the odML" in funding_diff
    assert "award_number" not in funding_diff
    assert person_diff == (f"Person {shared_person.id} (space 'common'): digital_identifiers is missing in the KG, "
                           "given in the odML; affiliations is missing in the KG, given in the odML")

    consortium = types.SimpleNamespace(id="https://kg.ebrains.eu/api/instances/c-1", space="X")
    schema = {"fairgraph_class": "core.Consortium", "kg_lookup": {"fields": ["full_name"]},
              "fields": {"full_name": {"odml": "full name"}}, "nested": {}}
    builder = _builder(doc, target_space="X")
    _fake_kg(builder, monkeypatch, {fgcore.Consortium: consortium})
    rebuilt = builder.build_with_nested("Consortium", schema, {"full name": "C"})
    assert rebuilt is not consortium and rebuilt.id == consortium.id and not builder.keep_existing_ids
    kept = builder.build_with_nested("Consortium", {**schema, "keep_existing": True}, {"full name": "C"})
    assert kept is consortium and builder.collection.keep_existing_ids == {consortium.id}
    assert _builder(doc).collection.kg_differences == []


def test_one_node_per_kg_identity_and_identifier(make_doc, monkeypatch):
    """The PI as Custodian and Contributor with one KG id is one Person (lists merged) with one ORCID node."""
    pi = {"given name": "Ada", "family name": "Lovelace", "affiliation": {"institution": "Uni"},
          "digital identifier": {"type": "ORCID", "value": "0000-0000-0000-0001"}}
    doc = make_doc(Dataset={"Custodian": pi, "Contributors": [{**pi, "affiliation": {"institution": "Other"}}]})
    builder = _builder(doc)
    person = types.SimpleNamespace(id="https://kg.ebrains.eu/api/instances/person-1", space=None)
    found = {fgcore.Person: person}
    monkeypatch.setattr(builder.kg, "lookup_kg", lambda fg_class, *args, **kwargs: found.get(fg_class))
    collection = builder.build_all()
    node = collection.nodes[person.id]
    assert [o.identifier for o in collection.nodes.values() if isinstance(o, fgcore.ORCID)] == [
        "https://orcid.org/0000-0000-0000-0001"]
    assert len(node.digital_identifiers) == 1
    assert sorted(a.member_of.full_name for a in node.affiliations) == ["Other", "Uni"]


def test_kg_lookup_and_entity_errors_raise(doc, monkeypatch):
    """Offline no lookup; a failed lookup or an unbuildable entity raises; only exact matches count."""
    def fail(**kwargs):
        raise ConnectionError("KG down")
    monkeypatch.setattr(fgcore.Person, "list", fail)
    assert KGResolver(client=None).lookup_kg(fgcore.Person, ["family_name"], {"family_name": "Doe"}) is None
    with pytest.raises(ConnectionError, match="KG down"):
        KGResolver(client=object()).lookup_kg(fgcore.Person, ["family_name"], {"family_name": "Doe"})
    monkeypatch.setattr(fgcore.Person, "list", lambda **kwargs: [types.SimpleNamespace(family_name=n, id=n)
                                                                 for n in ("Doe-Smith", "Doe")])
    assert KGResolver(client=object()).lookup_kg(fgcore.Person, ["family_name"], {"family_name": "Doe"}).id == "Doe"
    assert KGResolver(client=object()).lookup_kg(fgcore.Person, ["family_name"], {"family_name": "Do"}) is None

    def bad_init(**kwargs):
        raise TypeError("missing required arg")
    with pytest.raises(ValueError, match="BadEntity: cannot build .*missing required arg"):
        _builder(doc)._build_entity(bad_init, {}, None, True, "BadEntity")


def test_build_collection_needs_a_kg_space_with_a_client(make_doc, monkeypatch, tmp_path):
    """With a client the lookups need a space: the given one, else the collection metadata's, else an error."""
    monkeypatch.setattr(KGResolver, "lookup_kg", lambda self, *args, **kwargs: None)
    _, xml = save_odml(make_doc(), tmp_path / "ds")
    with pytest.raises(ValueError, match="No KG space.*--kg-space.*'kg space'"):
        build_collection(xml, kg_client=object())
    _, xml = save_odml(make_doc(Dataset={"kg space": "my-space"}), tmp_path / "ds2")
    assert build_collection(xml, kg_client=object()).kg_space == "my-space"


def test_kg_token_sources(tmp_path, monkeypatch):
    """The token from EBRAINS_KG_TOKEN, else the token file; none is an error; a rejected one says how to renew."""
    monkeypatch.delenv(kg.TOKEN_ENV, raising=False)
    monkeypatch.setattr(kg, "TOKEN_FILE", tmp_path / "kg_token")
    assert kg.read_token() is None
    with pytest.raises(ValueError, match="EBRAINS_KG_TOKEN"):
        kg.get_kg_client()
    (tmp_path / "kg_token").write_text("from-file\n")
    assert kg.read_token() == "from-file"
    monkeypatch.setenv(kg.TOKEN_ENV, "from-env")
    assert kg.read_token() == "from-env"

    def reject(**kwargs):
        raise AuthenticationError()
    monkeypatch.setattr(kg, "KGClient", reject)
    with pytest.raises(AuthenticationError, match="expired"):
        kg.get_kg_client()


class _Collection:
    """The parts of a fairgraph Collection the push reads."""

    def __init__(self, nodes, **attrs):
        self._nodes = nodes
        self.__dict__.update(attrs)

    def validate(self):
        return {}

    def sort_nodes_for_upload(self):
        return self._nodes


def _node(node_id, space=None, errors=(), calls=None):
    """A node whose save raises the given errors in turn, then succeeds."""
    errors = list(errors)

    def save(client, **kwargs):
        if calls is not None:
            calls.append(1)
        if errors:
            raise errors.pop(0)
    return types.SimpleNamespace(id=node_id, space=space, save=save)


def test_push_collection(tmp_path, monkeypatch):
    """Push space, fresh log, skipped nodes, typed rejections, retries; an expired token stops the push."""
    monkeypatch.setattr(kg, "sleep", lambda seconds: None)
    with pytest.raises(ValueError, match="No KG space"):
        kg.push_collection(_Collection([]), client=None)
    (tmp_path / kg.UPLOAD_LOG).write_text("stale\n")
    kept, other = "https://kg.ebrains.eu/api/instances/kept", "https://kg.ebrains.eu/api/instances/other"
    nodes = [_node("_:1"), _node(kept, "s"), _node(other, "common"), _node("_:2", errors=[Exception("code=500")]),
             _node("_:3", errors=[AuthorizationError("403")]), _node("_:4", errors=[ResourceExistsError("409")]),
             _node("_:5", errors=[Exception("code=400")] * 3)]
    collection = _Collection(nodes, kg_space="s", output_dir=tmp_path, keep_existing_ids={kept})
    result = kg.push_collection(collection, client=None)
    assert result == {"saved": 2, "skipped": 2, "errors": 3}
    assert (tmp_path / kg.UPLOAD_LOG).read_text() == "_:1\n_:2\n"
    calls = []
    with pytest.raises(AuthenticationError):
        kg.push_collection(_Collection([_node("_:1", errors=[AuthenticationError("401")] * 3, calls=calls)]),
                           client=None, space="s")
    assert len(calls) == 1


FDEF = {"type": "custom_anatomy", "odml_field": "brain region", "area_field": "atlas area",
        "uberon_field": "uberon term", "hemisphere_field": "hemisphere"}
ATLAS = {"M1": {"area": "4a", "uberon term": "primary_motor_cortex"}}


def test_anatomical_entities():
    """One entity per (area, UBERON term, hemisphere): placement fields, then atlas, then the label."""
    anatomy = Anatomy(Collection(), KGResolver(None))
    entity, = anatomy.build(FDEF, {"brain region": "primary visual cortex", "hemisphere": "left"})
    assert (entity.name, entity.related_uberon_term.name, entity.has_annotations[0].lateralities[0].name) == (
        "primary visual cortex", "primary visual cortex", "left")
    unknown, = anatomy.build(FDEF, {"brain region": "M1"})
    assert unknown.related_uberon_term is None and unknown.has_annotations is None
    assert anatomy.build(FDEF, {"brain region": "M1"}) == [unknown]
    assert anatomy.build(FDEF, {"hemisphere": "left"}) is None

    atlas = Anatomy(Collection(), KGResolver(None), ATLAS)
    entity, = atlas.build(FDEF, {"brain region": "M1"})
    assert (entity.name, entity.related_uberon_term.name) == ("4a", "primary motor cortex")
    entity, = atlas.build(FDEF, {"brain region": "M1", "atlas area": "F1", "uberon term": "premotor_cortex"})
    assert (entity.name, entity.related_uberon_term.name) == ("F1", "premotor cortex")


def test_atlas_is_optional(doc, tmp_path):
    """No atlas named means none; a named one sits next to the collection metadata or is an error."""
    shared = tmp_path / "collection" / "metadata"
    shared.mkdir(parents=True)
    out = tmp_path / "datasets" / "d" / "curated-data" / "openminds"
    _, xml = save_odml(compile_to_odml({"Dataset": {"short name": "ds"}}), out / "plain")
    assert atlas_path(odml.load(str(xml)), xml) is None and load_atlas(None) == {}
    named = compile_to_odml({"Dataset": {"short name": "ds", ATLAS_FIELD: "_atlas.yaml"}})
    with pytest.raises(FileNotFoundError, match="_atlas.yaml"):
        atlas_path(named, xml)
    (shared / "_atlas.yaml").write_text(yaml.safe_dump(ATLAS))
    assert load_atlas(atlas_path(named, xml)) == ATLAS

    _, xml = save_odml(doc, tmp_path / "ds")
    areas = lambda c: [n.name for n in c.nodes.values() if type(n).__name__ == "CustomAnatomicalEntity"]
    assert areas(build_collection(xml, kg_client=None, atlas=shared / "_atlas.yaml")) == ["4a"]
    assert areas(build_collection(xml, kg_client=None)) == ["M1"]


def test_openminds_enrichment_of_the_odml():
    """Term properties get their openMINDS IRI and instance; an unresolvable term is a warning."""
    doc = compile_to_odml({"Subject": {"species": "Macaca mulatta", "subject id": "A", "biological sex": ""},
                           "Dataset": {"license": "CC-BY-4.0", "full name": "T"},
                           "Sessions": [{"session id": "01", "preparation design": "in vivo"},
                                        {"session id": "02", "preparation design": "Not A Real Term"}]})
    warnings, instances = openminds_enrich(doc)
    species = doc["Subject"].properties["species"]
    assert species.reference == "https://openminds.om-i.org/props/species"
    assert doc["Dataset"].properties["full name"].reference == "https://openminds.om-i.org/props/fullName"
    assert "instances/species" in instances["Subject"]["species"]["Macaca mulatta"]["instance_uri"]
    assert "instances/licenses" in instances["Dataset"]["license"]["CC-BY-4.0"]["instance_uri"]
    assert "in vivo" in instances["Sessions_1"]["preparation design"]
    assert len(warnings) == 1 and "Not A Real Term" in warnings[0]

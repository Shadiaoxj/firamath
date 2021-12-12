'''Build FiraMath.glyphspackage.
'''

import copy
import functools
import multiprocessing
import os
import sys
import time

from fontmake.font_project import FontProject
from fontmake.instantiator import Instantiator

import fontTools
from fontTools.designspaceLib import DesignSpaceDocument
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables import otTables
from fontTools.ttLib.ttFont import newTable

import glyphsLib
from glyphsLib import GSComponent, GSFont, GSGlyph, GSLayer, GSNode, GSPath
from glyphsLib.parser import Parser

import toml


class Font:

    def __init__(self, path: str):
        self.font = self._load_pkg(path)
        self.math_tables: dict[str, MathTable] = {}
        masters = sorted(self.font.masters, key=lambda m: m.weightValue)
        self._masters_num = len(masters)
        self._master_id_indices = {m.id: i for i, m in enumerate(masters)}
        self.interpolations: dict[str, tuple] = {
            i.name: [
                (self._master_id_indices[id], value)
                for id, value in i.instanceInterpolations.items()
            ]
            for i in self.font.instances if i.active
        }
        self._decompose_smart_comp()

    @staticmethod
    def _load_pkg(path: str) -> GSFont:
        '''Load `.glyphspackage` bundle.
        See [googlefonts/glyphsLib#643](https://github.com/googlefonts/glyphsLib/issues/643).
        '''
        with open(os.path.join(path, 'fontinfo.plist'), 'r') as fontinfo_plist:
            fontinfo = fontinfo_plist.read()
        with open(os.path.join(path, 'order.plist'), 'r') as order_plist:
            order = Parser().parse(order_plist.read())
        insert_pos = fontinfo.find('instances = (')
        glyphs = ',\n'.join(Font._read_glyph(path, name) for name in order)
        glyphs = f'glyphs = (\n{glyphs}\n);\n'
        return glyphsLib.loads(fontinfo[:insert_pos] + glyphs + fontinfo[insert_pos:-1])

    @staticmethod
    def _read_glyph(path: str, name: str) -> str:
        if name == '.notdef':
            file_name = '_notdef.glyph'
        else:
            file_name = ''.join(c + '_' if c.isupper() else c for c in name) + '.glyph'
        with open(os.path.join(path, 'glyphs', file_name), 'r') as f:
            return f.read()[:-1]

    def _decompose_smart_comp(self):
        '''Decompose smart components.
        See [googlefonts/glyphsLib#91](https://github.com/googlefonts/glyphsLib/issues/91).
        '''
        # The smart glyphs should be decomposed first.
        for glyph in filter(Font._is_smart_glyph, self.font.glyphs):
            for layer in glyph.layers:
                to_be_removed = []
                for comp in layer.components:
                    if self._is_smart_component(comp):
                        paths = self._smart_component_to_paths(comp)
                    else:
                        paths = self._component_to_paths(comp)
                    layer.paths.extend(paths)
                    to_be_removed.append(comp)
                layer._shapes = [s for s in layer._shapes if s not in to_be_removed]
        for glyph in filter(lambda g: not Font._is_smart_glyph(g), self.font.glyphs):
            for layer in glyph.layers:
                to_be_removed = []
                for comp in layer.components:
                    if comp.smartComponentValues:
                        paths = self._smart_component_to_paths(comp)
                        layer.paths.extend(paths)
                        to_be_removed.append(comp)
                layer._shapes = [s for s in layer._shapes if s not in to_be_removed]

    @staticmethod
    def _is_smart_glyph(glyph: GSGlyph) -> bool:
        return glyph.smartComponentAxes != []

    @staticmethod
    def _is_smart_component(comp: GSComponent) -> bool:
        return Font._is_smart_glyph(comp.component)

    @staticmethod
    def _smart_component_to_paths(comp: GSComponent) -> list[GSPath]:
        '''Return the paths of a smart component `comp` by interpolating between two layers.
        Note that we only consider single smart component axis here.
        '''
        values: dict = comp.smartComponentValues
        master_id: str = comp.parent.associatedMasterId
        ref_glyph: GSGlyph = comp.component
        if len(values) == 0:
            interpolation_value = 0
            def _is_part_n(layer, n):
                return (
                    layer.associatedMasterId == master_id and
                    layer.partSelection[next(iter(layer.partSelection.keys()))] == n
                )
        elif len(values) == 1:
            key, value = next(iter(values.items()))
            interpolation_value = next(
                Font._rescale(value, axis.bottomValue, axis.topValue)
                for axis in ref_glyph.smartComponentAxes if axis.name == key
            )
            def _is_part_n(layer, n):
                return layer.associatedMasterId == master_id and layer.partSelection[key] == n
        else:
            raise ValueError('We only support single smart component axis!')
        layer_0: GSLayer = next(layer for layer in ref_glyph.layers if _is_part_n(layer, 1))
        layer_1: GSLayer = next(layer for layer in ref_glyph.layers if _is_part_n(layer, 2))
        paths = []
        for path_0, path_1 in zip(layer_0.paths, layer_1.paths):
            path = Font._interpolate_path(path_0, path_1, interpolation_value)
            path.applyTransform(comp.transform)
            paths.append(path)
        return paths

    @staticmethod
    def _rescale(x, bottom, top):
        '''Return rescaled `x` to run from 0 to 1 over the range `bottom` to `top`.'''
        return (x - bottom) / (top - bottom)

    @staticmethod
    def _interpolate_path(path_0: GSPath, path_1: GSPath, value) -> GSPath:
        new_path = copy.copy(path_0)
        new_path.nodes = []
        for node_0, node_1 in zip(path_0.nodes, path_1.nodes):
            new_path.nodes.append(Font._interpolate_node(node_0, node_1, value))
        return new_path

    @staticmethod
    def _interpolate_node(node_0: GSNode, node_1: GSNode, value) -> GSNode:
        position = (
            round(node_0.position.x * (1 - value) + node_1.position.x * value),
            round(node_0.position.y * (1 - value) + node_1.position.y * value),
        )
        return GSNode(position, type=node_0.type, smooth=node_0.smooth)

    @staticmethod
    def _component_to_paths(comp: GSComponent) -> list[GSPath]:
        '''Return the paths of a normal component `comp`, i.e. decompose `comp`.'''
        ref_glyph: GSGlyph = comp.component
        paths = next(
            layer.paths for layer in ref_glyph.layers
            if layer.layerId == comp.parent.associatedMasterId
        )
        result = []
        for path in paths:
            # Manually deepcopy (`copy.deepcopy()` is very slow here).
            new_path = copy.copy(path)
            new_path.nodes = [GSNode(n.position, type=n.type, smooth=n.smooth) for n in path.nodes]
            new_path.applyTransform(comp.transform)
            result.append(new_path)
        return result

    def to_ufos(self, interpolate: bool = True, default_index: int = None) -> list:
        master_ufos, instance_data = glyphsLib.to_ufos(self.font, include_instances=True)
        if not interpolate:
            return master_ufos
        designspace: DesignSpaceDocument = instance_data['designspace']
        if default_index:
            designspace.default = designspace.sources[default_index]
        else:
            designspace.default = next(
                (s for s in designspace.sources if s.styleName == 'Regular'),
                designspace.sources[0]
            )
        for axis_index, _ in enumerate(designspace.axes):
            positions = [i.axes[axis_index] for i in self.font.instances]
            designspace.axes[axis_index].map = None
            designspace.axes[axis_index].maximum = max(positions)
            designspace.axes[axis_index].minimum = min(positions)
            designspace.axes[axis_index].default = next(
                i.axes[axis_index] for i in self.font.instances if isinstance(i.weight, str)
            )
        instantiator = Instantiator.from_designspace(designspace)
        return [self._generate_instance(instantiator, i) for i in designspace.instances]

    @staticmethod
    def _generate_instance(instantiator: Instantiator, instance: list):
        ufo = instantiator.generate_instance(instance)
        if custom_parameters := instance.lib.get('com.schriftgestaltung.customParameters'):
            if remove_glyphs := dict(custom_parameters).get('Remove Glyphs'):
                ufo.lib['public.skipExportGlyphs'] = remove_glyphs
        for glyph in (g for g in ufo if '.BRACKET.' in g.name):
            glyph.lib['com.schriftgestaltung.Glyphs.Export'] = False
        return ufo

    def add_math_table(self, toml_path: str, input_dir: str, output_dir: str = None):
        if not output_dir:
            output_dir = input_dir
        if not os.path.isdir(output_dir):
            os.mkdir(output_dir)

        font_name = self.font.familyName.replace(' ', '')
        self._parse_math_table(toml_path)

        for style in self.interpolations:
            font_file_name = f'{font_name}-{style}.otf'
            input_path = os.path.join(input_dir, font_file_name)
            output_path = os.path.join(output_dir, font_file_name)
            with TTFont(input_path) as tt_font:
                tt_font['MATH'] = newTable('MATH')
                tt_font['MATH'].table = self.math_tables[style].encode()
                tt_font.save(output_path)

    def _parse_math_table(self, toml_path: str):
        master_data = self._parse_master_math_table(toml_path)
        master_glyph_info = master_data['MathGlyphInfo']
        master_variants = master_data['MathVariants']

        for style, interpolation in self.interpolations.items():
            instance = next(i for i in self.font.instances if i.name == style)
            remove_glyphs = instance.customParameters['Remove Glyphs']
            def _is_removed_glyph(glyph):
                return glyph in remove_glyphs if remove_glyphs else False
            def _generate(values):
                return round(sum(values[i] * v for i, v in interpolation))
            def _variant(name):
                return {
                    glyph: {g: _generate(values) for g, values in variants.items()}
                    for glyph, variants in master_variants[name].items()
                }
            def _componet(name):
                return {
                    glyph: {
                        # TODO: need to be interpolated
                        'italicsCorrection': componet['italicsCorrection'],
                        'parts': [
                            {
                                'name': part['name'],
                                'isExtender': part['isExtender'],
                                'startConnector': _generate(part['startConnector']),
                                'endConnector': _generate(part['endConnector']),
                                'fullAdvance': _generate(part['fullAdvance']),
                            }
                            for part in componet['parts']
                        ]
                    }
                    for glyph, componet in master_variants[name].items()
                }
            math_table = MathTable()
            for name, d in master_data['MathConstants'].items():
                math_table.constants[name] = {
                    'value': _generate(d['value']),
                    'isMathValue': d['isMathValue'],
                }
            for name in ['ItalicCorrection', 'TopAccent']:
                # TODO: consider brace layers
                math_table.glyph_info[name] = {
                    g: _generate(values)
                    for g, values in master_glyph_info[name].items() if not _is_removed_glyph(g)
                }
            math_table.glyph_info['ExtendedShapes'] = master_glyph_info['ExtendedShapes']
            math_table.variants['MinConnectorOverlap'] = \
                _generate(master_variants['MinConnectorOverlap'])
            math_table.variants['HorizontalVariants'] = _variant('HorizontalVariants')
            math_table.variants['VerticalVariants'] = _variant('VerticalVariants')
            math_table.variants['HorizontalComponents'] = _componet('HorizontalComponents')
            math_table.variants['VerticalComponents'] = _componet('VerticalComponents')
            self.math_tables[style] = math_table

    def _parse_master_math_table(self, toml_path: str) -> dict:
        data = toml.load(toml_path)
        glyph_info = data['MathGlyphInfo']
        variants = data['MathVariants']
        for name in glyph_info:
            for glyph, values in self._get_all_user_data(name).items():
                if len(values) != self._masters_num:
                    # TODO:
                    print(
                        f'Warning: glyph "{glyph}" has incomplete '
                        f'MathGlyphInfo ({name}: {values}).',
                        file=sys.stderr
                    )
                    values = [values[0]] * self._masters_num
                glyph_info[name][glyph] = values
        for glyph, value in variants['HorizontalVariants'].items():
            variants['HorizontalVariants'][glyph] = {
                var: self._advances(var, 'H', plus_1=True)
                for var in (glyph + suffix for suffix in value['suffixes'])
            }
        for glyph, value in variants['VerticalVariants'].items():
            variants['VerticalVariants'][glyph] = {
                var: self._advances(var, 'V', plus_1=True)
                for var in (glyph + suffix for suffix in value['suffixes'])
            }
        for glyph, value in variants['HorizontalComponents'].items():
            variants['HorizontalComponents'][glyph]['parts'] = [
                part | self._variant_part(part['name'], 'H') for part in value['parts']
            ]
        for glyph, value in variants['VerticalComponents'].items():
            variants['VerticalComponents'][glyph]['parts'] = [
                part | self._variant_part(part['name'], 'V') for part in value['parts']
            ]
        return data

    def _get_all_user_data(self, name: str) -> dict[str, list]:
        # Uncapitalize: 'TopAccent' -> 'topAccent', etc.
        name = name[0].lower() + name[1:]
        mappings = {}
        for glyph in (g for g in self.font.glyphs if g.export):
            values = self._get_user_data(glyph, name)
            if values:
                mappings[glyph.name] = values
        return mappings

    def _get_user_data(self, glyph: GSGlyph, name: str) -> list:
        values = []
        for layer in self._master_layers(glyph.layers):
            # Assume there is only one `name` in layer.userData
            try:
                data = next(d for d in layer.userData if name in d)
                values.append(data[name])
            except StopIteration:
                pass
        return values

    def _master_layers(self, layers) -> list[GSLayer]:
        return sorted(
            (l for l in layers if l.associatedMasterId == l.layerId),
            key=lambda l: self._master_id_indices[l.associatedMasterId]
        )

    def _advances(self, glyph: str, direction: str, plus_1: bool = False) -> list:
        result = []
        for layer in self._master_layers(self.font.glyphs[glyph].layers):
            size = layer.bounds.size
            advance = size.width if direction == 'H' else size.height
            result.append(abs(round(advance)))
        if plus_1:
            return [i + 1 for i in result]
        return result

    def _variant_part(self, glyph: str, direction: str) -> dict[str, list]:
        result = {
            name: self._get_user_data(self.font.glyphs[glyph], name)
            for name in ['startConnector', 'endConnector']
        }
        result['fullAdvance'] = self._advances(glyph, direction)
        return result


class MathTable:

    def __init__(self):
        self.constants = {}
        self.glyph_info = {
            'ItalicCorrection': {},
            'TopAccent': {},
            'ExtendedShapes': [],
        }
        self.variants = {}

    def encode(self):
        table = otTables.MATH()
        table.Version = 0x00010000
        table.MathConstants = self._encode_constants()
        table.MathGlyphInfo = self._encode_glyph_info()
        table.MathVariants = self._encode_variants()
        return table

    def _encode_constants(self):
        constants = otTables.MathConstants()
        for name, d in self.constants.items():
            value = d['value']
            constants.__setattr__(name, self._math_value(value) if d['isMathValue'] else value)
        return constants

    def _encode_glyph_info(self):
        italic_corr = otTables.MathItalicsCorrectionInfo()
        italic_corr.ItalicsCorrection, italic_corr.Coverage, italic_corr.ItalicsCorrectionCount = \
            self._glyph_info('ItalicCorrection')
        top_accent = otTables.MathTopAccentAttachment()
        top_accent.TopAccentAttachment, top_accent.TopAccentCoverage, top_accent.TopAccentAttachmentCount = \
            self._glyph_info('TopAccent')
        glyph_info = otTables.MathGlyphInfo()
        glyph_info.MathItalicsCorrectionInfo = italic_corr
        glyph_info.MathTopAccentAttachment = top_accent
        glyph_info.ExtendedShapeCoverage = self._coverage(self.glyph_info['ExtendedShapes'])
        glyph_info.MathKernInfo = None
        return glyph_info

    def _glyph_info(self, name: str):
        return (
            list(map(self._math_value, self.glyph_info[name].values())),
            self._coverage(self.glyph_info[name].keys()),
            len(self.glyph_info[name])
        )

    def _encode_variants(self):
        variants = otTables.MathVariants()
        variants.MinConnectorOverlap = self.variants['MinConnectorOverlap']
        variants.HorizGlyphConstruction, variants.HorizGlyphCoverage, variants.HorizGlyphCount = \
            self._variants('Horizontal')
        variants.VertGlyphConstruction, variants.VertGlyphCoverage, variants.VertGlyphCount = \
            self._variants('Vertical')
        return variants

    def _variants(self, name: str):
        constructions = {}
        for glyph, variants in self.variants[name + 'Variants'].items():
            constructions[glyph] = self._glyph_construction(variants)
        for glyph, component in self.variants[name + 'Components'].items():
            if glyph not in constructions:
                constructions[glyph] = self._glyph_construction({})
            constructions[glyph].GlyphAssembly = self._glyph_assembly(component)
        return constructions.values(), self._coverage(constructions.keys()), len(constructions)

    @staticmethod
    def _glyph_construction(variants: dict):
        construction = otTables.MathGlyphConstruction()
        construction.GlyphAssembly = None
        construction.VariantCount = len(variants)
        construction.MathGlyphVariantRecord = []
        for glyph, advance in variants.items():
            r = otTables.MathGlyphVariantRecord()
            r.VariantGlyph = glyph
            r.AdvanceMeasurement = advance
            construction.MathGlyphVariantRecord.append(r)
        return construction

    @staticmethod
    def _glyph_assembly(component: dict):
        t = otTables.GlyphAssembly()
        t.ItalicsCorrection = MathTable._math_value(component['italicsCorrection'])
        t.PartCount = len(component['parts'])
        t.PartRecords = []
        for part in component['parts']:
            r = otTables.GlyphPartRecord()
            r.glyph = part['name']
            r.StartConnectorLength = part['startConnector']
            r.EndConnectorLength = part['endConnector']
            r.FullAdvance = part['fullAdvance']
            r.PartFlags = 0x0001 if part['isExtender'] else 0xFFFE
            t.PartRecords.append(r)
        return t

    @staticmethod
    def _math_value(value):
        t = otTables.MathValueRecord()
        t.DeviceTable = None
        t.Value = value
        return t

    @staticmethod
    def _coverage(glyphs):
        c = otTables.Coverage()
        c.glyphs = glyphs
        return c


class Timer:

    def __init__(self, name=None, file=sys.stderr):
        self.name = name
        self.file = file
        self.start_time = None

    def __enter__(self):
        if self.name:
            print(self.name, file=self.file)
        self.start_time = time.time()

    def __exit__(self, exc_type, exc_val, exc_tb):
        t = time.time() - self.start_time
        if t < 60:
            print(f'Elapsed: {t:.3f}s\n', file=self.file)
        else:
            print(f'Elapsed: {int(t) // 60}min{(t % 60):.3f}s\n', file=self.file)


def build(input_path: str, toml_path: str, output_dir: str, parallel: bool = True):
    '''Build fonts from Glyphs source.

    1. Read the `.glyphspackage` directory into a `GSFont` object with preprocessing
    2. Convert the `GSFont` into a list of UFO objects and perform interpolation
    3. Generate `.otf` font files
    4. Add the OpenType MATH tables
    '''
    print(
        f'Python: {sys.version.split()[0]}\n'
        f'fonttools: {fontTools.version}\n'
        f'glyphsLib: {glyphsLib.__version__}\n'
        f'CPU count: {multiprocessing.cpu_count()}\n',
        file=sys.stderr
    )
    with Timer(f'Parsing input file "{input_path}"...'):
        font = Font(input_path)
    with Timer('Generating UFO...'):
        ufos = font.to_ufos()
    with Timer('Generating OTF...'):
        _build = functools.partial(_build_otf, output_dir=output_dir)
        if parallel:
            with multiprocessing.Pool() as p:
                p.map(_build, ufos)
        else:
            _build_otf(ufos, output_dir)
    with Timer('Adding MATH table...'):
        font.add_math_table(toml_path, input_dir=output_dir)


def _build_otf(ufo, output_dir):
    ufos = ufo if isinstance(ufo, list) else [ufo]
    FontProject(verbose='WARNING').build_otfs(ufos, output_dir=output_dir)


if __name__ == '__main__':
    build('src/FiraMath.glyphspackage', toml_path='src/FiraMath.toml', output_dir='build/')

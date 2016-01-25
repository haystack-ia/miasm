#!/usr/bin/env python
#-*- coding:utf-8 -*-

import logging
import inspect
import re
from collections import namedtuple

import miasm2.expression.expression as m2_expr
from miasm2.expression.simplifications import expr_simp
from miasm2.expression.modint import moduint, modint
from miasm2.core.utils import Disasm_Exception, pck
from miasm2.core.graph import DiGraph
from miasm2.core.interval import interval


log_asmbloc = logging.getLogger("asmblock")
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter("%(levelname)-5s: %(message)s"))
log_asmbloc.addHandler(console_handler)
log_asmbloc.setLevel(logging.WARNING)


def is_int(a):
    return isinstance(a, int) or isinstance(a, long) or \
        isinstance(a, moduint) or isinstance(a, modint)


def expr_is_label(e):
    return isinstance(e, m2_expr.ExprId) and isinstance(e.name, asm_label)


def expr_is_int_or_label(e):
    return isinstance(e, m2_expr.ExprInt) or \
        (isinstance(e, m2_expr.ExprId) and isinstance(e.name, asm_label))


class asm_label:

    "Stand for an assembly label"

    def __init__(self, name="", offset=None):
        self.fixedblocs = False
        if is_int(name):
            name = "loc_%.16X" % (int(name) & 0xFFFFFFFFFFFFFFFF)
        self.name = name
        self.attrib = None
        if offset is None:
            self.offset = offset
        else:
            self.offset = int(offset)

    def __str__(self):
        if isinstance(self.offset, (int, long)):
            return "%s:0x%08x" % (self.name, self.offset)
        else:
            return "%s:%s" % (self.name, str(self.offset))

    def __repr__(self):
        rep = '<asmlabel '
        if self.name:
            rep += repr(self.name) + ' '
        rep += '>'
        return rep


class asm_raw:

    def __init__(self, raw=""):
        self.raw = raw

    def __str__(self):
        return repr(self.raw)


class asm_constraint(object):
    c_to = "c_to"
    c_next = "c_next"

    def __init__(self, label, c_t=c_to):
        # Sanity check
        assert isinstance(label, asm_label)

        self.label = label
        self.c_t = c_t

    def __str__(self):
        return "%s:%s" % (str(self.c_t), str(self.label))


class asm_constraint_next(asm_constraint):

    def __init__(self, label):
        super(asm_constraint_next, self).__init__(
            label, c_t=asm_constraint.c_next)


class asm_constraint_to(asm_constraint):

    def __init__(self, label):
        super(asm_constraint_to, self).__init__(
            label, c_t=asm_constraint.c_to)


class asm_bloc(object):

    def __init__(self, label, alignment=1):
        assert isinstance(label, asm_label)
        self.bto = set()
        self.lines = []
        self.label = label
        self.alignment = alignment

    def __str__(self):
        out = []
        out.append(str(self.label))
        for l in self.lines:
            out.append(str(l))
        if self.bto:
            lbls = ["->"]
            for l in self.bto:
                if l is None:
                    lbls.append("Unknown? ")
                else:
                    lbls.append(str(l) + " ")
            lbls = '\t'.join(lbls)
            out.append(lbls)
        return '\n'.join(out)

    def addline(self, l):
        self.lines.append(l)

    def addto(self, c):
        assert isinstance(self.bto, set)
        self.bto.add(c)

    def split(self, offset, l):
        log_asmbloc.debug('split at %x', offset)
        i = -1
        offsets = [x.offset for x in self.lines]
        if not l.offset in offsets:
            log_asmbloc.warning(
                'cannot split bloc at %X ' % offset +
                'middle instruction? default middle')
            offsets.sort()
            return None
        new_bloc = asm_bloc(l)
        i = offsets.index(offset)

        self.lines, new_bloc.lines = self.lines[:i], self.lines[i:]
        flow_mod_instr = self.get_flow_instr()
        log_asmbloc.debug('flow mod %r', flow_mod_instr)
        c = asm_constraint(l, asm_constraint.c_next)
        # move dst if flowgraph modifier was in original bloc
        # (usecase: split delayslot bloc)
        if flow_mod_instr:
            for xx in self.bto:
                log_asmbloc.debug('lbl %s', xx)
            c_next = set(
                [x for x in self.bto if x.c_t == asm_constraint.c_next])
            c_to = [x for x in self.bto if x.c_t != asm_constraint.c_next]
            self.bto = set([c] + c_to)
            new_bloc.bto = c_next
        else:
            new_bloc.bto = self.bto
            self.bto = set([c])
        return new_bloc

    def get_range(self):
        """Returns the offset hull of an asm_bloc"""
        if len(self.lines):
            return (self.lines[0].offset,
                    self.lines[-1].offset + self.lines[-1].l)
        else:
            return 0, 0

    def get_offsets(self):
        return [x.offset for x in self.lines]

    def add_cst(self, offset, c_t, symbol_pool):
        if isinstance(offset, (int, long)):
            l = symbol_pool.getby_offset_create(offset)
        elif isinstance(offset, str):
            l = symbol_pool.getby_name_create(offset)
        elif isinstance(offset, asm_label):
            l = offset
        else:
            raise ValueError('unknown offset type %r' % offset)
        c = asm_constraint(l, c_t)
        self.bto.add(c)

    def get_flow_instr(self):
        if not self.lines:
            return None
        for i in xrange(-1, -1 - self.lines[0].delayslot - 1, -1):
            if not 0 <= i < len(self.lines):
                return None
            l = self.lines[i]
            if l.splitflow() or l.breakflow():
                raise NotImplementedError('not fully functional')

    def get_subcall_instr(self):
        if not self.lines:
            return None
        delayslot = self.lines[0].delayslot
        end_index = len(self.lines) - 1
        ds_max_index = max(end_index - delayslot, 0)
        for i in xrange(end_index, ds_max_index - 1, -1):
            l = self.lines[i]
            if l.is_subcall():
                return l
        return None

    def get_next(self):
        for x in self.bto:
            if x.c_t == asm_constraint.c_next:
                return x.label
        return None

    @staticmethod
    def _filter_constraint(constraints):
        """Sort and filter @constraints for asm_bloc.bto
        @constraints: non-empty set of asm_constraint instance

        Always the same type -> one of the constraint
        c_next and c_to -> c_next
        """
        # Only one constraint
        if len(constraints) == 1:
            return next(iter(constraints))

        # Constraint type -> set of corresponding constraint
        cbytype = {}
        for cons in constraints:
            cbytype.setdefault(cons.c_t, set()).add(cons)

        # Only one type -> any constraint is OK
        if len(cbytype) == 1:
            return next(iter(constraints))

        # At least 2 types -> types = {c_next, c_to}
        # c_to is included in c_next
        return next(iter(cbytype[asm_constraint.c_next]))

    def fix_constraints(self):
        """Fix next block constraints"""
        # destination -> associated constraints
        dests = {}
        for constraint in self.bto:
            dests.setdefault(constraint.label, set()).add(constraint)

        self.bto = set(self._filter_constraint(constraints)
                       for constraints in dests.itervalues())


class asm_block_bad(asm_bloc):
    """Stand for a *bad* ASM block (malformed, unreachable,
    not disassembled, ...)"""

    ERROR_TYPES = {-1: "Unknown error",
                   0: "Unable to disassemble",
                   1: "Reach a null starting block",
    }

    def __init__(self, label=None, alignment=1, errno=-1, *args, **kwargs):
        """Instanciate an asm_block_bad.
        @label, @alignement: same as asm_bloc.__init__
        @errno: (optional) specify a error type associated with the block
        """
        super(asm_block_bad, self).__init__(label, alignment, *args, **kwargs)
        self._errno = errno

    def __str__(self):
        error_txt = self.ERROR_TYPES.get(self._errno, self._errno)
        return "\n".join([str(self.label),
                          "\tBad block: %s" % error_txt])

    def addline(self, *args, **kwargs):
        raise RuntimeError("An asm_block_bad cannot have line")

    def addto(self, *args, **kwargs):
        raise RuntimeError("An asm_block_bad cannot have bto")

    def split(self, *args, **kwargs):
        raise RuntimeError("An asm_block_bad cannot be splitted")


class asm_symbol_pool:

    def __init__(self):
        self._labels = []
        self._name2label = {}
        self._offset2label = {}
        self._label_num = 0

    def add_label(self, name, offset=None):
        """
        Create and add a label to the symbol_pool
        @name: label's name
        @offset: (optional) label's offset
        """
        label = asm_label(name, offset)

        # Test for collisions
        if (label.offset in self._offset2label and
                label != self._offset2label[label.offset]):
            raise ValueError('symbol %s has same offset as %s' %
                             (label, self._offset2label[label.offset]))
        if (label.name in self._name2label and
                label != self._name2label[label.name]):
            raise ValueError('symbol %s has same name as %s' %
                             (label, self._name2label[label.name]))

        self._labels.append(label)
        if label.offset is not None:
            self._offset2label[label.offset] = label
        if label.name != "":
            self._name2label[label.name] = label
        return label

    def remove_label(self, label):
        """
        Delete a @label
        """
        self._name2label.pop(label.name, None)
        self._offset2label.pop(label.offset, None)
        if label in self._labels:
            self._labels.remove(label)

    def del_label_offset(self, label):
        """Unpin the @label from its offset"""
        self._offset2label.pop(label.offset, None)
        label.offset = None

    def getby_offset(self, offset):
        """Retrieve label using its @offset"""
        return self._offset2label.get(offset, None)

    def getby_name(self, name):
        """Retrieve label using its @name"""
        return self._name2label.get(name, None)

    def getby_name_create(self, name):
        """Get a label from its @name, create it if it doesn't exist"""
        label = self.getby_name(name)
        if label is None:
            label = self.add_label(name)
        return label

    def getby_offset_create(self, offset):
        """Get a label from its @offset, create it if it doesn't exist"""
        label = self.getby_offset(offset)
        if label is None:
            label = self.add_label(offset, offset)
        return label

    def rename_label(self, label, newname):
        """Rename the @label name to @newname"""
        if newname in self._name2label:
            raise ValueError('Symbol already known')
        self._name2label.pop(label.name, None)
        label.name = newname
        self._name2label[label.name] = label

    def set_offset(self, label, offset):
        """Pin the @label from at @offset
        Note that there is a special case when the offset is a list
        it happens when offsets are recomputed in resolve_symbol*
        """
        if label is None:
            raise ValueError('label should not be None')
        if not label.name in self._name2label:
            raise ValueError('label %s not in symbol pool' % label)
        if offset is not None and offset in self._offset2label:
            raise ValueError('Conflict in label %s' % label)
        self._offset2label.pop(label.offset, None)
        label.offset = offset
        if is_int(label.offset):
            self._offset2label[label.offset] = label

    @property
    def items(self):
        """Return all labels"""
        return self._labels

    def __str__(self):
        return reduce(lambda x, y: x + str(y) + '\n', self._labels, "")

    def __getitem__(self, item):
        if item in self._name2label:
            return self._name2label[item]
        if item in self._offset2label:
            return self._offset2label[item]
        raise KeyError('unknown symbol %r' % item)

    def __contains__(self, item):
        return item in self._name2label or item in self._offset2label

    def merge(self, symbol_pool):
        """Merge with another @symbol_pool"""
        self._labels += symbol_pool._labels
        self._name2label.update(symbol_pool._name2label)
        self._offset2label.update(symbol_pool._offset2label)

    def gen_label(self):
        """Generate a new unpinned label"""
        label = self.add_label("lbl_gen_%.8X" % (self._label_num))
        self._label_num += 1
        return label


def dis_bloc(mnemo, pool_bin, label, offset, job_done, symbol_pool,
             dont_dis=[], split_dis=[
             ], follow_call=False, dontdis_retcall=False, lines_wd=None,
             dis_bloc_callback=None, dont_dis_nulstart_bloc=False,
             attrib={}):
    # pool_bin.offset = offset
    lines_cpt = 0
    in_delayslot = False
    delayslot_count = mnemo.delayslot
    offsets_to_dis = set()
    add_next_offset = False
    cur_block = asm_bloc(label)
    log_asmbloc.debug("dis at %X", int(offset))
    while not in_delayslot or delayslot_count > 0:
        if in_delayslot:
            delayslot_count -= 1

        if offset in dont_dis or (lines_cpt > 0 and offset in split_dis):
            cur_block.add_cst(offset, asm_constraint.c_next, symbol_pool)
            offsets_to_dis.add(offset)
            break

        lines_cpt += 1
        if lines_wd is not None and lines_cpt > lines_wd:
            # log_asmbloc.warning( "lines watchdog reached at %X"%int(offset))
            break

        if offset in job_done:
            cur_block.add_cst(offset, asm_constraint.c_next, symbol_pool)
            break

        off_i = offset
        try:
            instr = mnemo.dis(pool_bin, attrib, offset)
        except (Disasm_Exception, IOError), e:
            log_asmbloc.warning(e)
            instr = None

        if instr is None:
            log_asmbloc.warning("cannot disasm at %X", int(off_i))
            if not cur_block.lines:
                # Block is empty -> bad block
                cur_block = asm_block_bad(label, errno=0)
            else:
                # Block is not empty, stop the desassembly pass and add a
                # constraint to the next block
                cur_block.add_cst(off_i, asm_constraint.c_next, symbol_pool)
            break

        # XXX TODO nul start block option
        if dont_dis_nulstart_bloc and instr.b.count('\x00') == instr.l:
            log_asmbloc.warning("reach nul instr at %X", int(off_i))
            if not cur_block.lines:
                # Block is empty -> bad block
                cur_block = asm_block_bad(label, errno=1)
            else:
                # Block is not empty, stop the desassembly pass and add a
                # constraint to the next block
                cur_block.add_cst(off_i, asm_constraint.c_next, symbol_pool)
            break

        # special case: flow graph modificator in delayslot
        if in_delayslot and instr and (instr.splitflow() or instr.breakflow()):
            add_next_offset = True
            break

        job_done.add(offset)
        log_asmbloc.debug("dis at %X", int(offset))

        offset += instr.l
        log_asmbloc.debug(instr)
        log_asmbloc.debug(instr.args)

        cur_block.addline(instr)
        if not instr.breakflow():
            continue
        # test split
        if instr.splitflow() and not (instr.is_subcall() and dontdis_retcall):
            add_next_offset = True
            # cur_bloc.add_cst(n, asm_constraint.c_next, symbol_pool)
            pass
        if instr.dstflow():
            instr.dstflow2label(symbol_pool)
            dst = instr.getdstflow(symbol_pool)
            dstn = []
            for d in dst:
                if isinstance(d, m2_expr.ExprId) and \
                        isinstance(d.name, asm_label):
                    dstn.append(d.name)
            dst = dstn
            if (not instr.is_subcall()) or follow_call:
                cur_block.bto.update(
                    [asm_constraint(x, asm_constraint.c_to) for x in dst])

        # get in delayslot mode
        in_delayslot = True
        delayslot_count = instr.delayslot

    for c in cur_block.bto:
        offsets_to_dis.add(c.label.offset)

    if add_next_offset:
        cur_block.add_cst(offset, asm_constraint.c_next, symbol_pool)
        offsets_to_dis.add(offset)

    # Fix multiple constraints
    cur_block.fix_constraints()

    if dis_bloc_callback is not None:
        dis_bloc_callback(mn=mnemo, attrib=attrib, pool_bin=pool_bin,
                          cur_bloc=cur_block, offsets_to_dis=offsets_to_dis,
                          symbol_pool=symbol_pool)
    # print 'dst', [hex(x) for x in offsets_to_dis]
    return cur_block, offsets_to_dis


def split_bloc(mnemo, attrib, pool_bin, blocs,
               symbol_pool, more_ref=None, dis_bloc_callback=None):
    if not more_ref:
        more_ref = []

    # get all possible dst
    bloc_dst = [symbol_pool._offset2label[x] for x in more_ref]
    for b in blocs:
        if isinstance(b, asm_block_bad):
            continue
        for c in b.bto:
            bloc_dst.append(c.label)

    bloc_dst = [x.offset for x in bloc_dst if x.offset is not None]

    j = -1
    while j < len(blocs) - 1:
        j += 1
        cb = blocs[j]
        a, b = cb.get_range()

        for off in bloc_dst:
            if not (off > a and off < b):
                continue
            l = symbol_pool.getby_offset_create(off)
            new_b = cb.split(off, l)
            log_asmbloc.debug("split bloc %x", off)
            if new_b is None:
                log_asmbloc.error("cannot split %x!!", off)
                continue
            if dis_bloc_callback:
                offsets_to_dis = set(x.label.offset for x in new_b.bto)
                dis_bloc_callback(mn=mnemo, attrib=attrib, pool_bin=pool_bin,
                                  cur_bloc=new_b, offsets_to_dis=offsets_to_dis,
                                  symbol_pool=symbol_pool)
            blocs.append(new_b)
            a, b = cb.get_range()

    return blocs


def dis_bloc_all(mnemo, pool_bin, offset, job_done, symbol_pool, dont_dis=[],
                 split_dis=[], follow_call=False, dontdis_retcall=False,
                 blocs_wd=None, lines_wd=None, blocs=None,
                 dis_bloc_callback=None, dont_dis_nulstart_bloc=False,
                 attrib={}):
    log_asmbloc.info("dis bloc all")
    if blocs is None:
        blocs = BasicBlocks()
    todo = [offset]

    bloc_cpt = 0
    while len(todo):
        bloc_cpt += 1
        if blocs_wd is not None and bloc_cpt > blocs_wd:
            log_asmbloc.debug("blocs watchdog reached at %X", int(offset))
            break

        n = int(todo.pop(0))
        if n is None:
            continue
        if n in job_done:
            continue

        if n in dont_dis:
            continue
        dd_flag = False
        for dd in dont_dis:
            if not isinstance(dd, tuple):
                continue
            dd_a, dd_b = dd
            if dd_a <= n < dd_b:
                dd_flag = True
                break
        if dd_flag:
            continue
        label = symbol_pool.getby_offset_create(n)
        cur_block, nexts = dis_bloc(mnemo, pool_bin, label, n, job_done,
                                    symbol_pool, dont_dis, split_dis,
                                    follow_call, dontdis_retcall,
                                    dis_bloc_callback=dis_bloc_callback,
                                    lines_wd=lines_wd,
                                    dont_dis_nulstart_bloc=dont_dis_nulstart_bloc,
                                    attrib=attrib)
        todo += nexts
        blocs.add_node(cur_block)

    return split_bloc(mnemo, attrib, pool_bin, blocs,
                      symbol_pool, dis_bloc_callback=dis_bloc_callback)
    return blocs


class BasicBlocks(DiGraph):
    """Directed graph standing for a ASM Control Flow Graph with:
     - nodes: asm_bloc
     - edges: constraints between blocks, synchronized with asm_bloc's "bto"

    Specialized the .dot export and force the relation between block to be uniq,
    and associated with a constraint.

    Offer helpers on BasicBlocks management, such as research by label, sanity
    checking and mnemonic size guessing.
    """

    # Internal structure for pending management
    BasicBlocksPending = namedtuple("BasicBlocksPending",
                                    ["waiter", "constraint"])

    def __init__(self, *args, **kwargs):
        super(BasicBlocks, self).__init__(*args, **kwargs)
        # Edges -> constraint
        self.edges2constraint = {}
        # Expected asm_label -> list( (src, dst), constraint )
        self._pendings = {}
        # Label2block built on the fly
        self._label2block = {}

    # Compatibility with old list API
    def append(self, *args, **kwargs):
        raise DeprecationWarning("BasicBlocks is a graph, use add_node")

    def remove(self, *args, **kwargs):
        raise DeprecationWarning("BasicBlocks is a graph, use del_node")

    def __getitem__(self, *args, **kwargs):
        raise DeprecationWarning("Order of BasicBlocks elements is not reliable")

    def __iter__(self):
        """Iterator on asm_bloc composing the current graph"""
        return iter(self._nodes)

    def __len__(self):
        """Return the number of blocks in BasicBlocks"""
        return len(self._nodes)

    # Manage graph with associated constraints
    def add_edge(self, src, dst, constraint):
        """Add an edge to the graph
        @src: asm_bloc instance, source
        @dst: asm_block instance, destination
        @constraint: constraint associated to this edge
        """
        # Sanity check
        assert (src, dst) not in self.edges2constraint

        # Add the edge to src.bto if needed
        if dst.label not in [cons.label for cons in src.bto]:
            src.bto.add(asm_constraint(dst.label, constraint))

        # Add edge
        self.edges2constraint[(src, dst)] = constraint
        super(BasicBlocks, self).add_edge(src, dst)

    def add_uniq_edge(self, src, dst, constraint):
        """Add an edge from @src to @dst if it doesn't already exist"""
        if (src not in self._nodes_succ or
            dst not in self._nodes_succ[src]):
            self.add_edge(src, dst, constraint)

    def del_edge(self, src, dst):
        """Delete the edge @src->@dst and its associated constraint"""
        # Delete from src.bto
        to_remove = [cons for cons in src.bto if cons.label == dst.label]
        if to_remove:
            assert len(to_remove) == 1
            src.bto.remove(to_remove[0])

        # Del edge
        del self.edges2constraint[(src, dst)]
        super(BasicBlocks, self).del_edge(src, dst)

    def add_node(self, block):
        """Add the block @block to the current instance, if it is not already in
        @block: asm_bloc instance

        Edges will be created for @block.bto, if destinations are already in
        this instance. If not, they will be resolved when adding these
        aforementionned destinations.
        `self.pendings` indicates which blocks are not yet resolved.
        """
        status = super(BasicBlocks, self).add_node(block)
        if not status:
            return status

        # Update waiters
        if block.label in self._pendings:
            for bblpend in self._pendings[block.label]:
                self.add_edge(bblpend.waiter, block, bblpend.constraint)
            del self._pendings[block.label]

        # Synchronize edges with block destinations
        self._label2block[block.label] = block
        for constraint in block.bto:
            dst = self._label2block.get(constraint.label,
                                        None)
            if dst is None:
                # Block is yet unknown, add it to pendings
                to_add = self.BasicBlocksPending(waiter=block,
                                                 constraint=constraint.c_t)
                self._pendings.setdefault(constraint.label,
                                          list()).append(to_add)
            else:
                # Block is already in known nodes
                self.add_edge(block, dst, constraint.c_t)

        return status

    def del_node(self, block):
        super(BasicBlocks, self).del_node(block)
        del self._label2block[block.label]

    def merge(self, graph):
        """Merge with @graph, taking in account constraints"""
        # -> add_edge(x, y, constraint)
        for node in graph._nodes:
            self.add_node(node)
        for edge in graph._edges:
            # Use "_uniq_" beacause the edge can already exist due to add_node
            self.add_uniq_edge(*edge, constraint=graph.edges2constraint[edge])

    def dot(self, label=False, lines=True):
        """Render dot graph with HTML
        @label: (optional) if set, add the corresponding label in each block
        @lines: (optional) if set, includes assembly lines in the output
        """

        escape_chars = re.compile('[' + re.escape('{}') + ']')
        label_attr = 'colspan="2" align="center" bgcolor="grey"'
        edge_attr = 'label = "%s" color="%s" style="bold"'
        td_attr = 'align="left"'
        block_attr = 'shape="Mrecord" fontname="Courier New"'

        out = ["digraph asm_graph {"]
        fix_chars = lambda x: '\\' + x.group()

        # Generate basic blocks
        out_blocks = []
        for block in self.nodes():
            out_block = '%s [\n' % block.label.name
            out_block += "%s " % block_attr
            if isinstance(block, asm_block_bad):
                out_block += 'style=filled fillcolor="red" '
            out_block += 'label =<<table border="0" cellborder="0" cellpadding="3">'

            block_label = '<tr><td %s>%s</td></tr>' % (
                label_attr, block.label.name)
            block_html_lines = []

            if lines:
                if isinstance(block, asm_block_bad):
                    block_html_lines.append(block.ERROR_TYPES.get(block._errno,
                                                                  block._errno))

                for line in block.lines:
                    if label:
                        out_render = "%.8X</td><td %s> " % (line.offset,
                                                            td_attr)
                    else:
                        out_render = ""
                    out_render += escape_chars.sub(fix_chars, str(line))
                    block_html_lines.append(out_render)

            block_html_lines = ('<tr><td %s>' % td_attr +
                                ('</td></tr><tr><td %s>' % td_attr).join(block_html_lines) +
                                '</td></tr>')
            out_block += "%s " % block_label
            out_block += block_html_lines + "</table>> ];"
            out_blocks.append(out_block)

        out += out_blocks

        # Generate links
        for src, dst in self.edges():
            exp_label = dst.label
            cst = self.edges2constraint.get((src, dst), None)

            edge_color = "black"
            if cst == asm_constraint.c_next:
                edge_color = "red"
            elif cst == asm_constraint.c_to:
                edge_color = "limegreen"
            # special case
            if len(src.bto) == 1:
                edge_color = "blue"

            out.append('%s -> %s' % (src.label.name, dst.label.name) + \
                       '[' + edge_attr % (cst, edge_color) + '];')

        out.append("}")
        return '\n'.join(out)

    # Helpers
    @property
    def pendings(self):
        """Dictionnary of label -> list(BasicBlocksPending instance) indicating
        which label are missing in the current instance.
        A label is missing if a block which is already in nodes has constraints
        with him (thanks to its .bto) and the corresponding block is not yet in
        nodes
        """
        return self._pendings

    def _build_label2block(self):
        self._label2block = {block.label: block
                             for block in self._nodes}

    def label2block(self, label):
        """Return the block corresponding to label @label
        @label: asm_label instance or ExprId(asm_label) instance"""
        return self._label2block[label]

    def rebuild_edges(self):
        """Consider blocks '.bto' and rebuild edges according to them, ie:
        - update constraint type
        - add missing edge
        - remove no more used edge

        This method should be called if a block's '.bto' in nodes have been
        modified without notifying this instance to resynchronize edges.
        """
        self._build_label2block()
        for block in self._nodes:
            edges = []
            # Rebuild edges from bto
            for constraint in block.bto:
                dst = self._label2block.get(constraint.label,
                                            None)
                if dst is None:
                    # Missing destination, add to pendings
                    self._pendings.setdefault(constraint.label,
                                              list()).append(self.BasicBlocksPending(block,
                                                                                     constraint.c_t))
                    continue
                edge = (block, dst)
                edges.append(edge)
                if edge in self._edges:
                    # Already known edge, constraint may have changed
                    self.edges2constraint[edge] = constraint.c_t
                else:
                    # An edge is missing
                    self.add_edge(edge[0], edge[1], constraint.c_t)

            # Remove useless edges
            for succ in self.successors(block):
                edge = (block, succ)
                if edge not in edges:
                    self.del_edge(*edge)

    def get_bad_blocks(self):
        """Iterator on asm_block_bad elements"""
        # A bad asm block is always a leaf
        for block in self.leaves():
            if isinstance(block, asm_block_bad):
                yield block

    def get_bad_blocks_predecessors(self, strict=False):
        """Iterator on block with an asm_block_bad destination
        @strict: (optional) if set, return block with only bad
        successors
        """
        # Avoid returning the same block
        done = set()
        for badblock in self.get_bad_blocks():
            for predecessor in self.predecessors_iter(badblock):
                if predecessor not in done:
                    if (strict and
                        not all(isinstance(block, asm_block_bad)
                                for block in self.successors_iter(predecessor))):
                        continue
                    yield predecessor
                    done.add(predecessor)

    def sanity_check(self):
        """Do sanity checks on blocks' constraints:
        * no pendings
        * no multiple next constraint to same block
        * no next constraint to self
        """

        if len(self._pendings) != 0:
            raise RuntimeError("Some blocks are missing: %s" % map(str,
                                                                   self._pendings.keys()))

        next_edges = {edge: constraint
                      for edge, constraint in self.edges2constraint.iteritems()
                      if constraint == asm_constraint.c_next}

        for block in self._nodes:
            # No next constraint to self
            if (block, block) in next_edges:
                raise RuntimeError('Bad constraint: self in next')

            # No multiple next constraint to same block
            pred_next = list(pblock
                             for (pblock, dblock) in next_edges
                             if dblock == block)

            if len(pred_next) > 1:
                raise RuntimeError("Too many next constraints for bloc %r"
                                   "(%s)" % (block.label,
                                             map(lambda x: x.label, pred_next)))

    def guess_blocks_size(self, mnemo):
        """Asm and compute max block size
        Add a 'size' and 'max_size' attribute on each block
        @mnemo: metamn instance"""
        for block in self._nodes:
            size = 0
            for instr in block.lines:
                if isinstance(instr, asm_raw):
                    # for special asm_raw, only extract len
                    if isinstance(instr.raw, list):
                        data = None
                        if len(instr.raw) == 0:
                            l = 0
                        else:
                            l = instr.raw[0].size / 8 * len(instr.raw)
                    elif isinstance(instr.raw, str):
                        data = instr.raw
                        l = len(data)
                    else:
                        raise NotImplementedError('asm raw')
                else:
                    # Assemble the instruction to retrieve its len.
                    # If the instruction uses symbol it will fail
                    # In this case, the max_instruction_len is used
                    try:
                        candidates = mnemo.asm(instr)
                        l = len(candidates[-1])
                    except:
                        l = mnemo.max_instruction_len
                    data = None
                instr.data = data
                instr.l = l
                size += l

            block.size = size
            block.max_size = size
            log_asmbloc.info("size: %d max: %d", block.size, block.max_size)


def conservative_asm(mnemo, instr, symbols, conservative):
    """
    Asm instruction;
    Try to keep original instruction bytes if it exists
    """
    candidates = mnemo.asm(instr, symbols)
    if not candidates:
        raise ValueError('cannot asm:%s' % str(instr))
    if not hasattr(instr, "b"):
        return candidates[0], candidates
    if instr.b in candidates:
        return instr.b, candidates
    if conservative:
        for c in candidates:
            if len(c) == len(instr.b):
                return c, candidates
    return candidates[0], candidates


def fix_expr_val(expr, symbols):
    """Resolve an expression @expr using @symbols"""
    def expr_calc(e):
        if isinstance(e, m2_expr.ExprId):
            s = symbols._name2label[e.name]
            e = m2_expr.ExprInt_from(e, s.offset)
        return e
    result = expr.visit(expr_calc)
    result = expr_simp(result)
    if not isinstance(result, m2_expr.ExprInt):
        raise RuntimeError('Cannot resolve symbol %s' % expr)
    return result


def fix_label_offset(symbol_pool, label, offset, modified):
    """Fix the @label offset to @offset. If the @offset has changed, add @label
    to @modified
    @symbol_pool: current symbol_pool
    """
    if label.offset == offset:
        return
    symbol_pool.set_offset(label, offset)
    modified.add(label)


class BlockChain(object):

    """Manage blocks linked with an asm_constraint_next"""

    def __init__(self, symbol_pool, blocks):
        self.symbol_pool = symbol_pool
        self.blocks = blocks
        self.place()

    @property
    def pinned(self):
        """Return True iff at least one block is pinned"""
        return self.pinned_block_idx is not None

    def _set_pinned_block_idx(self):
        self.pinned_block_idx = None
        for i, block in enumerate(self.blocks):
            if is_int(block.label.offset):
                if self.pinned_block_idx is not None:
                    raise ValueError("Multiples pinned block detected")
                self.pinned_block_idx = i

    def place(self):
        """Compute BlockChain min_offset and max_offset using pinned block and
        blocks' size
        """
        self._set_pinned_block_idx()
        self.max_size = 0
        for block in self.blocks:
            self.max_size += block.max_size + block.alignment - 1

        # Check if chain has one block pinned
        if not self.pinned:
            return

        offset_base = self.blocks[self.pinned_block_idx].label.offset
        assert(offset_base % self.blocks[self.pinned_block_idx].alignment == 0)

        self.offset_min = offset_base
        for block in self.blocks[:self.pinned_block_idx - 1:-1]:
            self.offset_min -= block.max_size + \
                (block.alignment - block.max_size) % block.alignment

        self.offset_max = offset_base
        for block in self.blocks[self.pinned_block_idx:]:
            self.offset_max += block.max_size + \
                (block.alignment - block.max_size) % block.alignment

    def merge(self, chain):
        """Best effort merge two block chains
        Return the list of resulting blockchains"""
        self.blocks += chain.blocks
        self.place()
        return [self]

    def fix_blocks(self, modified_labels):
        """Propagate a pinned to its blocks' neighbour
        @modified_labels: store new pinned labels"""

        if not self.pinned:
            raise ValueError('Trying to fix unpinned block')

        # Propagate offset to blocks before pinned block
        pinned_block = self.blocks[self.pinned_block_idx]
        offset = pinned_block.label.offset
        if offset % pinned_block.alignment != 0:
            raise RuntimeError('Bad alignment')

        for block in self.blocks[:self.pinned_block_idx - 1:-1]:
            new_offset = offset - block.size
            new_offset = new_offset - new_offset % pinned_block.alignment
            fix_label_offset(self.symbol_pool,
                             block.label,
                             new_offset,
                             modified_labels)

        # Propagate offset to blocks after pinned block
        offset = pinned_block.label.offset + pinned_block.size

        last_block = pinned_block
        for block in self.blocks[self.pinned_block_idx + 1:]:
            offset += (- offset) % last_block.alignment
            fix_label_offset(self.symbol_pool,
                             block.label,
                             offset,
                             modified_labels)
            offset += block.size
            last_block = block
        return modified_labels


class BlockChainWedge(object):

    """Stand for wedges between blocks"""

    def __init__(self, symbol_pool, offset, size):
        self.symbol_pool = symbol_pool
        self.offset = offset
        self.max_size = size
        self.offset_min = offset
        self.offset_max = offset + size

    def merge(self, chain):
        """Best effort merge two block chains
        Return the list of resulting blockchains"""
        self.symbol_pool.set_offset(chain.blocks[0].label, self.offset_max)
        chain.place()
        return [self, chain]


def group_constrained_blocks(symbol_pool, blocks):
    """
    Return the BlockChains list built from grouped asm blocks linked by
    asm_constraint_next
    @blocks: a list of asm block
    """
    log_asmbloc.info('group_constrained_blocks')

    # Group adjacent blocks
    remaining_blocks = list(blocks)
    known_block_chains = {}
    lbl2block = {block.label: block for block in blocks}

    while remaining_blocks:
        # Create a new block chain
        block_list = [remaining_blocks.pop()]

        # Find sons in remainings blocks linked with a next constraint
        while True:
            # Get next block
            next_label = block_list[-1].get_next()
            if next_label is None or next_label not in lbl2block:
                break
            next_block = lbl2block[next_label]

            # Add the block at the end of the current chain
            if next_block not in remaining_blocks:
                break
            block_list.append(next_block)
            remaining_blocks.remove(next_block)

        # Check if son is in a known block group
        if next_label is not None and next_label in known_block_chains:
            block_list += known_block_chains[next_label]
            del known_block_chains[next_label]

        known_block_chains[block_list[0].label] = block_list

    out_block_chains = []
    for label in known_block_chains:
        chain = BlockChain(symbol_pool, known_block_chains[label])
        out_block_chains.append(chain)
    return out_block_chains


def get_blockchains_address_interval(blockChains, dst_interval):
    """Compute the interval used by the pinned @blockChains
    Check if the placed chains are in the @dst_interval"""

    allocated_interval = interval()
    for chain in blockChains:
        if not chain.pinned:
            continue
        chain_interval = interval([(chain.offset_min, chain.offset_max - 1)])
        if chain_interval not in dst_interval:
            raise ValueError('Chain placed out of destination interval')
        allocated_interval += chain_interval
    return allocated_interval


def resolve_symbol(blockChains, symbol_pool, dst_interval=None):
    """Place @blockChains in the @dst_interval"""

    log_asmbloc.info('resolve_symbol')
    if dst_interval is None:
        dst_interval = interval([(0, 0xFFFFFFFFFFFFFFFF)])

    forbidden_interval = interval(
        [(-1, 0xFFFFFFFFFFFFFFFF + 1)]) - dst_interval
    allocated_interval = get_blockchains_address_interval(blockChains,
                                                          dst_interval)
    log_asmbloc.debug('allocated interval: %s', allocated_interval)

    pinned_chains = [chain for chain in blockChains if chain.pinned]

    # Add wedge in forbidden intervals
    for start, stop in forbidden_interval.intervals:
        wedge = BlockChainWedge(
            symbol_pool, offset=start, size=stop + 1 - start)
        pinned_chains.append(wedge)

    # Try to place bigger blockChains first
    pinned_chains.sort(key=lambda x: x.offset_min)
    blockChains.sort(key=lambda x: -x.max_size)

    fixed_chains = list(pinned_chains)

    log_asmbloc.debug("place chains")
    for chain in blockChains:
        if chain.pinned:
            continue
        fixed = False
        for i in xrange(1, len(fixed_chains)):
            prev_chain = fixed_chains[i - 1]
            next_chain = fixed_chains[i]

            if prev_chain.offset_max + chain.max_size < next_chain.offset_min:
                new_chains = prev_chain.merge(chain)
                fixed_chains[i - 1:i] = new_chains
                fixed = True
                break
        if not fixed:
            raise RuntimeError('Cannot find enough space to place blocks')

    return [chain for chain in fixed_chains if isinstance(chain, BlockChain)]


def filter_exprid_label(exprs):
    """Extract labels from list of ExprId @exprs"""
    return set(expr.name for expr in exprs if isinstance(expr.name, asm_label))


def get_block_labels(block):
    """Extract labels used by @block"""
    symbols = set()
    for instr in block.lines:
        if isinstance(instr, asm_raw):
            if isinstance(instr.raw, list):
                for expr in instr.raw:
                    symbols.update(m2_expr.get_expr_ids(expr))
        else:
            for arg in instr.args:
                symbols.update(m2_expr.get_expr_ids(arg))
    labels = filter_exprid_label(symbols)
    return labels


def assemble_block(mnemo, block, symbol_pool, conservative=False):
    """Assemble a @block using @symbol_pool
    @conservative: (optional) use original bytes when possible
    """
    offset_i = 0

    for instr in block.lines:
        if isinstance(instr, asm_raw):
            if isinstance(instr.raw, list):
                # Fix special asm_raw
                data = ""
                for expr in instr.raw:
                    expr_int = fix_expr_val(expr, symbol_pool)
                    data += pck[expr_int.size](expr_int.arg)
                instr.data = data

            instr.offset = offset_i
            offset_i += instr.l
            continue

        # Assemble an instruction
        saved_args = list(instr.args)
        instr.offset = block.label.offset + offset_i

        # Replace instruction's arguments by resolved ones
        instr.args = instr.resolve_args_with_symbols(symbol_pool)

        if instr.dstflow():
            instr.fixDstOffset()

        old_l = instr.l
        cached_candidate, candidates = conservative_asm(
            mnemo, instr, symbol_pool, conservative)

        # Restore original arguments
        instr.args = saved_args

        # We need to update the block size
        block.size = block.size - old_l + len(cached_candidate)
        instr.data = cached_candidate
        instr.l = len(cached_candidate)

        offset_i += instr.l


def asmbloc_final(mnemo, blocks, blockChains, symbol_pool, conservative=False):
    """Resolve and assemble @blockChains using @symbol_pool until fixed point is
    reached"""

    log_asmbloc.debug("asmbloc_final")

    # Init structures
    lbl2block = {block.label: block for block in blocks}
    blocks_using_label = {}
    for block in blocks:
        labels = get_block_labels(block)
        for label in labels:
            blocks_using_label.setdefault(label, set()).add(block)

    block2chain = {}
    for chain in blockChains:
        for block in chain.blocks:
            block2chain[block] = chain

    # Init worklist
    blocks_to_rework = set(blocks)

    # Fix and re-assemble blocks until fixed point is reached
    while True:

        # Propagate pinned blocks into chains
        modified_labels = set()
        for chain in blockChains:
            chain.fix_blocks(modified_labels)

        for label in modified_labels:
            # Retrive block with modified reference
            if label in lbl2block:
                blocks_to_rework.add(lbl2block[label])

            # Enqueue blocks referencing a modified label
            if label not in blocks_using_label:
                continue
            for block in blocks_using_label[label]:
                blocks_to_rework.add(block)

        # No more work
        if not blocks_to_rework:
            break

        while blocks_to_rework:
            block = blocks_to_rework.pop()
            assemble_block(mnemo, block, symbol_pool, conservative)


def asm_resolve_final(mnemo, blocks, symbol_pool, dst_interval=None):
    """Resolve and assemble @blocks using @symbol_pool into interval
    @dst_interval"""

    blocks.sanity_check()

    blocks.guess_blocks_size(mnemo)
    blockChains = group_constrained_blocks(symbol_pool, blocks)
    resolved_blockChains = resolve_symbol(
        blockChains, symbol_pool, dst_interval)

    asmbloc_final(mnemo, blocks, resolved_blockChains, symbol_pool)
    patches = {}
    output_interval = interval()

    for block in blocks:
        offset = block.label.offset
        for instr in block.lines:
            if not instr.data:
                # Empty line
                continue
            assert len(instr.data) == instr.l
            patches[offset] = instr.data
            instruction_interval = interval([(offset, offset + instr.l - 1)])
            if not (instruction_interval & output_interval).empty:
                raise RuntimeError("overlapping bytes %X" % int(offset))
            instr.offset = offset
            offset += instr.l
    return patches




def find_parents(blocs, l):
    p = set()
    for b in blocs:
        if l in [x.label for x in b.bto]:
            p.add(b.label)
    return p


def bloc_blink(blocs):
    for b in blocs:
        b.parents = find_parents(blocs, b.label)


def getbloc_around(blocs, a, level=3, done=None, blocby_label=None):

    if not blocby_label:
        blocby_label = {}
        for b in blocs:
            blocby_label[b.label] = b
    if done is None:
        done = set()

    done.add(a)
    if not level:
        return done
    for b in a.parents:
        b = blocby_label[b]
        if b in done:
            continue
        done.update(getbloc_around(blocs, b, level - 1, done, blocby_label))
    for b in a.bto:
        b = blocby_label[b.label]
        if b in done:
            continue
        done.update(getbloc_around(blocs, b, level - 1, done, blocby_label))
    return done


def getbloc_parents(blocs, a, level=3, done=None, blocby_label=None):

    if not blocby_label:
        blocby_label = {}
        for b in blocs:
            blocby_label[b.label] = b
    if done is None:
        done = set()

    done.add(a)
    if not level:
        return done
    for b in a.parents:
        b = blocby_label[b]
        if b in done:
            continue
        done.update(getbloc_parents(blocs, b, level - 1, done, blocby_label))
    return done

# get ONLY level_X parents


def getbloc_parents_strict(
        blocs, a, level=3, rez=None, done=None, blocby_label=None):

    if not blocby_label:
        blocby_label = {}
        for b in blocs:
            blocby_label[b.label] = b
    if rez is None:
        rez = set()
    if done is None:
        done = set()

    done.add(a)
    if level == 0:
        rez.add(a)
    if not level:
        return rez
    for b in a.parents:
        b = blocby_label[b]
        if b in done:
            continue
        rez.update(getbloc_parents_strict(
            blocs, b, level - 1, rez, done, blocby_label))
    return rez


def bloc_find_path_next(blocs, blocby_label, a, b, path=None):
    if path == None:
        path = []
    if a == b:
        return [path]

    all_path = []
    for x in a.bto:
        if x.c_t != asm_constraint.c_next:
            continue
        if not x.label in blocby_label:
            log_asmbloc.error('XXX unknown label')
            continue
        x = blocby_label[x.label]
        all_path += bloc_find_path_next(blocs, blocby_label, x, b, path + [a])
        # stop if at least one path found
        if all_path:
            return all_path
    return all_path


def bloc_merge(blocs, dont_merge=[]):
    blocby_label = {}
    for b in blocs:
        blocby_label[b.label] = b
        b.parents = find_parents(blocs, b.label)

    i = -1
    while i < len(blocs) - 1:
        i += 1
        b = blocs[i]
        if b.label in dont_merge:
            continue
        p = set(b.parents)
        # if bloc dont self ref
        if b.label in p:
            continue
        # and bloc has only one parent
        if len(p) != 1:
            continue
        # may merge
        bpl = p.pop()
        # bp = getblocby_label(blocs, bpl)
        bp = blocby_label[bpl]
        # and parent has only one son
        if len(bp.bto) != 1:
            continue
        # and will not create next loop composed of constraint_next from son to
        # parent

        path = bloc_find_path_next(blocs, blocby_label, b, bp)
        if path:
            continue
        if bp.lines:
            l = bp.lines[-1]
            # jmp opt; jcc opt
            if l.is_subcall():
                continue
            if l.breakflow() and l.dstflow():
                bp.lines.pop()
        # merge
        # sons = b.bto[:]

        # update parents
        for s in b.bto:
            if s.label.name == None:
                continue
            if not s.label in blocby_label:
                log_asmbloc.error("unknown parent XXX")
                continue
            bs = blocby_label[s.label]
            for p in list(bs.parents):
                if p == b.label:
                    bs.parents.discard(p)
                    bs.parents.add(bp.label)
        bp.lines += b.lines
        bp.bto = b.bto

        del blocs[i]
        i = -1


class disasmEngine(object):

    def __init__(self, arch, attrib, bs=None, **kwargs):
        self.arch = arch
        self.attrib = attrib
        self.bs = bs
        self.symbol_pool = asm_symbol_pool()
        self.dont_dis = []
        self.split_dis = []
        self.follow_call = False
        self.dontdis_retcall = False
        self.lines_wd = None
        self.blocs_wd = None
        self.dis_bloc_callback = None
        self.dont_dis_nulstart_bloc = False
        self.job_done = set()
        self.__dict__.update(kwargs)

    def dis_bloc(self, offset):
        label = self.symbol_pool.getby_offset_create(offset)
        current_block, _ = dis_bloc(self.arch, self.bs, label, offset,
                                    self.job_done, self.symbol_pool,
                                    dont_dis=self.dont_dis,
                                    split_dis=self.split_dis,
                                    follow_call=self.follow_call,
                                    dontdis_retcall=self.dontdis_retcall,
                                    lines_wd=self.lines_wd,
                                    dis_bloc_callback=self.dis_bloc_callback,
                                    dont_dis_nulstart_bloc=self.dont_dis_nulstart_bloc,
                                    attrib=self.attrib)
        return current_block

    def dis_multibloc(self, offset, blocs=None):
        blocs = dis_bloc_all(self.arch, self.bs, offset, self.job_done,
                             self.symbol_pool,
                             dont_dis=self.dont_dis, split_dis=self.split_dis,
                             follow_call=self.follow_call,
                             dontdis_retcall=self.dontdis_retcall,
                             blocs_wd=self.blocs_wd,
                             lines_wd=self.lines_wd,
                             blocs=blocs,
                             dis_bloc_callback=self.dis_bloc_callback,
                             dont_dis_nulstart_bloc=self.dont_dis_nulstart_bloc,
                             attrib=self.attrib)
        return blocs
